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
        # B17 hooks.
        # stop_cap_disabled: NO-CAP MODE (Detail 3). When True, a
        # trailing stop sells its FULL nominal qty when it triggers,
        # even if that exceeds the shares held — the position goes
        # negative (a short is visible). The default cap (a stop never
        # sells more than the position) HIDES an oversized stop, so
        # the oversized-stop test must run with the cap disabled.
        # Default False: the cap is the broker-level backstop.
        self.stop_cap_disabled = False
        # drop_next_stop_cancel: the next cancel() for a trailing stop
        # is APPLIED (the stop's status becomes cancelled) but the
        # RESPONSE is dropped (TimeoutError) — the cancel
        # confirmation is lost (three-outcome trace (c)). The stop is
        # also hidden from the open-orders view for the rest of this
        # process (a broker whose order view lags the cancel), so the
        # caller's re-fetch cannot confirm it either; the next
        # process's startup lookup BY ORDER ID finds it cancelled.
        self.drop_next_stop_cancel = False
        # _hidden_stop_coids: stops hidden from list_stops() /
        # open_orders() for this process (see drop_next_stop_cancel).
        # get_by_client_order_id still finds them — that is the
        # point: absence from the open-orders list proves nothing.
        self._hidden_stop_coids: set[str] = set()
        # _stop_elect_during_submit: symbol -> (stop client_order_id,
        # fill price). When armed, the next submit() of a SELL for
        # that symbol first lets the stop elect (B17.4 race: the stop
        # fires between the cancel confirmation and the exit submit).
        # Consumed once.
        self._stop_elect_during_submit: dict[str, tuple] = {}

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

    def set_stop_cap_disabled(self, disabled: bool = True) -> None:
        """Enable/disable NO-CAP MODE (Detail 3). When disabled=False
        (the default), trigger_trailing_stop caps the fill at the
        shares held. When True, the stop sells its full nominal qty
        even if that exceeds the holding — the position goes negative
        and the short is visible. The oversized-stop test runs the
        SAME broker with the cap disabled before AND after the fix."""
        with self._lock:
            self.stop_cap_disabled = bool(disabled)

    def set_fill_during_stop_cancel(self, client_order_id: str, qty: float, price: float) -> None:
        """Arm a fill on a TRAILING STOP that lands while its
        cancellation is pending (three-outcome traces (a)/(b)): the
        next cancel() for this stop applies the fill FIRST, then
        cancels — so the cancel result reports the final filled qty.
        A full fill (qty = the stop's remaining qty) is outcome (a)
        (the stop fills before the cancel confirms); a partial fill
        is outcome (b) (the stop sells some shares, then the cancel
        confirms for the remainder). Same mechanism as
        set_fill_during_cancel, under a stop-specific name. Consumed
        once."""
        self.set_fill_during_cancel(client_order_id, qty, price)

    def set_drop_next_stop_cancel(self) -> None:
        """Arm the dropped-cancel fault (three-outcome trace (c)):
        the next cancel() for a trailing stop is applied but its
        response is dropped, and the stop is hidden from the
        open-orders view for the rest of this process. Consumed once."""
        with self._lock:
            self.drop_next_stop_cancel = True

    def set_stop_elect_during_submit(self, client_order_id: str, price: float) -> None:
        """Arm the B17.4 race: the next submit() of a SELL for the
        stop's symbol first lets this stop elect at `price` (it sells
        its qty), then proceeds with the submit. Consumed once. The
        stop is re-opened first (its cancel had not actually taken
        effect at the broker when the exit submitted) — that is the
        race: the cancel confirmation and the stop's election crossed."""
        with self._lock:
            stop = self._stops.get(client_order_id)
            if stop is None:
                raise KeyError(f"unknown trailing stop {client_order_id}")
            self._stop_elect_during_submit[str(stop.get("symbol"))] = (client_order_id, float(price))

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
            # B17.4 race: a stop armed to elect during this submit.
            # The stop fires FIRST (its cancel had not taken effect
            # at the broker), then the exit order is stored.
            elect = None
            if str(order.get("side", "")).lower() == "sell":
                elect = self._stop_elect_during_submit.pop(str(order.get("symbol")), None)
            if elect is not None:
                stop_coid, elect_price = elect
                stop = self._stops.get(stop_coid)
                if stop is not None and str(stop.get("status", "")).lower() in {
                        "cancelled", "canceled"}:
                    # The cancel confirmation and the election crossed:
                    # the stop was still executable when the exit
                    # submitted. Re-open it so the trigger can fire.
                    stop["status"] = "submitted"
                if stop is not None:
                    self._trigger_trailing_stop_locked(stop_coid, elect_price)
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
        lands first, so the cancel result is the final position.

        B17 (three-outcome trace (c)): when drop_next_stop_cancel is
        armed and this is a trailing stop, the cancel is APPLIED but
        the response is DROPPED (TimeoutError) and the stop is hidden
        from the open-orders view for the rest of this process — the
        caller cannot confirm the cancel, so the stop's state stays
        UNKNOWN and the exit stays blocked; the next process's startup
        lookup BY ORDER ID finds it cancelled."""
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
            if (self.drop_next_stop_cancel
                    and client_order_id in self._stops):
                self.drop_next_stop_cancel = False
                self._hidden_stop_coids.add(client_order_id)
                raise TimeoutError(
                    "fake broker cancelled stop but dropped response")
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

    def attach_trailing_stop(self, symbol: str, qty: float, trail_pct: float,
                             client_order_id: str | None = None) -> dict:
        """Attach a trailing-stop SELL for a held position (B16
        correction): a REAL broker order in its own store, so the
        strategy's exit can cancel it, the end-of-run reconcile can
        see its fills, and a test can elect it. Its id sequence is
        separate (fake-trail-NNNN) so it never consumes a strategy
        order id, and order_count()/list_orders() still count
        strategy submissions only.

        B17: when `client_order_id` is given (the strategy passes a
        deterministic "<entry intent id>-trail"), the stop is stored
        under that key, so it can be looked up BY ORDER ID. Re-
        attaching the same client_order_id while the existing stop is
        still WORKING returns it (idempotent — the strategy's resize
        never double-attaches). But when the existing stop with that
        coid is TERMINAL (cancelled / filled / expired / rejected),
        the re-attach creates a NEW stop (a new broker id) under the
        same coid with the new qty — the resize (cancel + re-attach)
        reuses the deterministic coid, and a real broker likewise
        lets a client_order_id be reused once the original order is
        terminal. Returning the old cancelled stop here would make
        the resize a no-op (the "new" stop would keep the old qty).
        """
        with self._lock:
            if client_order_id is not None and client_order_id in self._stops:
                existing = self._stops[client_order_id]
                if str(existing.get("status", "")).lower() not in {
                        "filled", "cancelled", "canceled", "expired", "rejected"}:
                    return dict(existing)
                # Terminal: fall through and create a new stop under
                # the same coid (a new broker id, the new qty).
            self._trail_seq += 1
            coid = client_order_id or f"trail-{symbol}-{self._trail_seq:04d}"
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
        """Elect a trailing stop (test 3): the stop sells. With the
        cap enabled (the default), the fill is capped at the shares
        actually held — a stop must never sell more than the position
        (the broker-level backstop; the strategy-level guarantee is
        cancel-before-exit). With NO-CAP MODE (stop_cap_disabled,
        Detail 3), the stop sells its FULL nominal qty even if that
        exceeds the holding — the position goes negative and the short
        is visible, so an oversized stop cannot hide behind the cap.
        A stop with nothing to sell fills zero and stays working."""
        with self._lock:
            return self._trigger_trailing_stop_locked(client_order_id, price)

    def _trigger_trailing_stop_locked(self, client_order_id: str, price: float) -> dict:
        """The trigger itself, with the lock already held (shared by
        trigger_trailing_stop and the elect-during-submit hook)."""
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
        remaining = float(stop["qty"]) - float(stop["filled_qty"])
        if self.stop_cap_disabled:
            # NO-CAP MODE (Detail 3): the stop sells its full nominal
            # remaining qty, even if that exceeds the shares held.
            # The position goes negative — a short is visible.
            fill_qty = max(0.0, remaining)
        else:
            held = self._held_qty(str(stop.get("symbol")))
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
            # Stops hidden by the dropped-cancel fault (B17, trace
            # (c)) are absent from this view — the open-orders list
            # cannot confirm them. get_by_client_order_id still
            # finds them by order id.
            return [dict(o) for coid, o in self._stops.items()
                    if coid not in self._hidden_stop_coids]

    def list_all_stops(self) -> list[dict]:
        """Every trailing stop, including ones hidden from the
        open-orders view (test inspection only)."""
        with self._lock:
            return [dict(o) for o in self._stops.values()]

    def duplicate_fill_delivery(self, fill: dict) -> dict:
        """Replay the exact same fill (same fill_id) — a duplicate delivery."""
        return dict(fill)

    def list_orders(self) -> list[dict]:
        with self._lock:
            return [dict(o) for o in self._orders.values()]

    def list_all_orders(self) -> list[dict]:
        """The account's FULL order history (strategy orders AND
        trailing stops) — the independent check a human resolution
        uses (B17.1): the broker's own records for the account over
        the relevant window."""
        with self._lock:
            return [dict(o) for o in self._orders.values()] + [dict(o) for o in self._stops.values()]

    def order_count(self) -> int:
        with self._lock:
            return len(self._orders)
