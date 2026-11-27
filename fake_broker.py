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
        # B16 correction: trailing stops are real broker orders, kept
        # in a separate dict so order_count() / list_orders() still
        # count STRATEGY submissions only (the B13/B15/B16 tests
        # assert those counts). A stop has its own id sequence
        # (fake-trail-NNNN) so it never consumes a strategy order id.
        self._stops: dict[str, dict] = {}  # client_order_id -> stop order
        self._trail_seq = 0
        # Test hooks (B16 correction): a fill that lands between the
        # cancel request and the broker's confirmation, and a genuine
        # submit failure (the broker raises before storing anything).
        self._fill_during_cancel: dict[str, tuple] = {}
        self.fail_next_submit = False

    def _find(self, client_order_id: str) -> Optional[dict]:
        """An order by client_order_id, searching strategy orders
        first, then trailing stops."""
        order = self._orders.get(client_order_id)
        if order is None:
            order = self._stops.get(client_order_id)
        return order

    def _held_qty(self, symbol: str) -> float:
        """Net filled qty for a symbol across all orders (buys +,
        sells -): what a trailing stop may sell without shorting."""
        net = 0.0
        for store in (self._orders, self._stops):
            for order in store.values():
                if str(order.get("symbol")) != symbol:
                    continue
                filled = float(order.get("filled_qty") or 0.0)
                net += filled if order.get("side") == "buy" else -filled
        return net

    def submit(self, order: dict) -> dict:
        coid = order.get("client_order_id")
        if not coid:
            raise ValueError("fake broker requires client_order_id")
        with self._lock:
            if self.fail_next_submit:
                # Genuine failure: the broker raises BEFORE storing
                # anything, so the order was never accepted — the
                # lookup will never find it (the UNKNOWN intent stays
                # blocked until a human resolves it; see test 3c).
                self.fail_next_submit = False
                raise ConnectionError("fake broker failure (injected)")
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
            order = self._find(client_order_id)
            if order is None:
                return None
            count = self._visibility_counts.get(client_order_id, 0) + 1
            self._visibility_counts[client_order_id] = count
            if count <= self.delayed_visibility_polls:
                return None
            return dict(order)

    def apply_partial_fill(self, client_order_id: str, fill_id: str, qty: float, price: float) -> dict:
        with self._lock:
            order = self._find(client_order_id)
            if order is None:
                raise KeyError(f"unknown order {client_order_id}")
            if fill_id in self._fills:
                return dict(self._fills[fill_id])
            # B16: a terminally-cancelled/expired order cannot fill —
            # the real broker never delivers a fill for it. A late
            # fill attempt is a no-op (zero-qty fill, nothing
            # recorded) instead of silently re-opening the position.
            # A done_for_day order is NOT terminal (Alpaca docs: it
            # "will not receive further updates until the next
            # trading day") — it CAN still fill, so fills are
            # accepted here and move it back to partially_filled /
            # filled.
            if str(order.get("status", "")).lower() in {
                    "cancelled", "canceled", "expired", "rejected"}:
                return {
                    "fill_id": fill_id,
                    "client_order_id": client_order_id,
                    "broker_order_id": order["id"],
                    "qty": 0.0,
                    "cumulative_filled_qty": float(order["filled_qty"]),
                    "price": float(price),
                }
            # B15: keep the volume-weighted average fill price on the
            # stored order, so open_orders() can price the unfilled
            # remainder (the filled_avg_price fallback). A zero-qty
            # fill leaves the average untouched. (Shared with the
            # fill-during-cancel hook via _apply_fill_locked.)
            return self._apply_fill_locked(order, client_order_id, fill_id, qty, price)

    def set_fill_during_cancel(self, client_order_id: str, qty: float, price: float) -> None:
        """Arm a fill that lands between the cancel request and the
        broker's confirmation (test 2): the next cancel() for this
        order applies the fill FIRST, then cancels — so the cancel
        result reports the final filled qty, not the pre-cancel
        snapshot. Consumed once. The fill is refused if the order is
        already terminal (a cancelled/expired order cannot fill)."""
        with self._lock:
            self._fill_during_cancel[client_order_id] = (float(qty), float(price))

    def cancel(self, client_order_id: str) -> dict:
        """Cancel an open order (B15): the unfilled remainder stops
        working, but shares already filled stay filled — they are a
        position, not part of the cancelled order. Cancelling a filled
        or already-cancelled order is a no-op (idempotent). A
        done_for_day order CAN still be cancelled (Alpaca docs: an
        order may be canceled up until it reaches filled, canceled,
        or expired) — and a fill armed via set_fill_during_cancel
        lands first, so the cancel result is the final position."""
        with self._lock:
            order = self._find(client_order_id)
            if order is None:
                raise KeyError(f"unknown order {client_order_id}")
            pending = self._fill_during_cancel.pop(client_order_id, None)
            if pending is not None:
                qty, price = pending
                if str(order.get("status", "")).lower() not in {
                        "filled", "cancelled", "canceled", "expired", "rejected"}:
                    self._apply_fill_locked(order, client_order_id,
                                            f"fill-during-cancel-{client_order_id}",
                                            qty, price)
            if str(order.get("status", "")).lower() not in {
                    "filled", "cancelled", "canceled", "rejected"}:
                order["status"] = "cancelled"
            return dict(order)

    def _apply_fill_locked(self, order: dict, client_order_id: str,
                           fill_id: str, qty: float, price: float) -> dict:
        """The fill itself, with the lock already held (shared by
        apply_partial_fill and the fill-during-cancel hook)."""
        if fill_id in self._fills:
            return dict(self._fills[fill_id])
        prev_filled = float(order["filled_qty"])
        prev_avg = order.get("filled_avg_price")
        new_qty = float(qty)
        filled = prev_filled + new_qty
        order["filled_qty"] = min(filled, float(order["qty"]))
        if new_qty > 0:
            if prev_filled > 0 and prev_avg is not None:
                order["filled_avg_price"] = (
                    float(prev_avg) * prev_filled + float(price) * new_qty
                ) / (prev_filled + new_qty)
            else:
                order["filled_avg_price"] = float(price)
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

    def expire(self, client_order_id: str) -> dict:
        """Mark an order expired (B16): a DAY order that reached the
        close unfilled. Filled shares stay filled; the remainder will
        never fill. Like a cancel, but with the broker's expiry status
        so the expiry path (not just the cancel path) is exercised.
        Expiring a filled or already-terminal order is a no-op."""
        with self._lock:
            order = self._find(client_order_id)
            if order is None:
                raise KeyError(f"unknown order {client_order_id}")
            if str(order.get("status", "")).lower() not in {
                    "filled", "cancelled", "canceled", "expired", "rejected"}:
                order["status"] = "expired"
            return dict(order)

    def done_for_day(self, client_order_id: str) -> dict:
        """Mark an order done_for_day (B16 correction): a DAY order
        that reached the close partially filled (or unfilled). Filled
        shares stay filled; the remainder is STILL WORKING — it can
        fill the next trading day, it still counts against buying
        power, and it can still be cancelled (Alpaca docs: done_for_day
        "will not receive further updates until the next trading
        day" — it is NOT terminal, unlike expired/canceled). Marking
        a filled or already-terminal order is a no-op."""
        with self._lock:
            order = self._find(client_order_id)
            if order is None:
                raise KeyError(f"unknown order {client_order_id}")
            if str(order.get("status", "")).lower() not in {
                    "filled", "cancelled", "canceled", "expired", "rejected",
                    "done_for_day"}:
                order["status"] = "done_for_day"
            return dict(order)

    def attach_trailing_stop(self, symbol: str, qty: float, trail_pct: float) -> dict:
        """Attach a trailing-stop SELL for a held position (B16
        correction): a REAL broker order in its own store, so the
        strategy's exit can cancel it, the end-of-run reconcile can
        see its fills, and a test can elect it. Its id sequence is
        separate (fake-trail-NNNN) so it never consumes a strategy
        order id, and order_count()/list_orders() still count
        strategy submissions only."""
        with self._lock:
            self._trail_seq += 1
            coid = f"trail-{symbol}-{self._trail_seq:04d}"
            stop = {
                "id": f"fake-trail-{self._trail_seq:04d}",
                "client_order_id": coid,
                "symbol": symbol,
                "side": "sell",
                "qty": float(qty),
                "order_type": "trailing_stop",
                "trail_pct": float(trail_pct),
                "status": "submitted",
                "filled_qty": 0.0,
            }
            self._stops[coid] = stop
            return dict(stop)

    def trigger_trailing_stop(self, client_order_id: str, price: float) -> dict:
        """Elect a trailing stop (test 3): the stop sells, capped at
        the shares actually held — a stop must never sell more than
        the position (the broker-level backstop; the strategy-level
        guarantee is cancel-before-exit). A stop with nothing to sell
        fills zero and stays working."""
        with self._lock:
            stop = self._stops.get(client_order_id)
            if stop is None:
                raise KeyError(f"unknown trailing stop {client_order_id}")
            if str(stop.get("status", "")).lower() in {
                    "filled", "cancelled", "canceled", "expired", "rejected"}:
                return {
                    "fill_id": f"stop-{client_order_id}-noop",
                    "client_order_id": client_order_id,
                    "broker_order_id": stop["id"],
                    "qty": 0.0,
                    "cumulative_filled_qty": float(stop["filled_qty"]),
                    "price": float(price),
                }
            held = self._held_qty(str(stop.get("symbol")))
            remaining = float(stop["qty"]) - float(stop["filled_qty"])
            fill_qty = max(0.0, min(remaining, held))
            if fill_qty <= 0:
                return {
                    "fill_id": f"stop-{client_order_id}-noop",
                    "client_order_id": client_order_id,
                    "broker_order_id": stop["id"],
                    "qty": 0.0,
                    "cumulative_filled_qty": float(stop["filled_qty"]),
                    "price": float(price),
                }
            return self._apply_fill_locked(
                stop, client_order_id,
                f"stop-fill-{client_order_id}-{int(stop['filled_qty'] + fill_qty)}",
                fill_qty, price)

    def list_stops(self) -> list[dict]:
        with self._lock:
            return [dict(o) for o in self._stops.values()]

    def duplicate_fill_delivery(self, fill: dict) -> dict:
        """Replay the exact same fill (same fill_id) — a duplicate delivery."""
        return dict(fill)

    def list_orders(self) -> list[dict]:
        with self._lock:
            return [dict(o) for o in self._orders.values()]

    def order_count(self) -> int:
        with self._lock:
            return len(self._orders)
