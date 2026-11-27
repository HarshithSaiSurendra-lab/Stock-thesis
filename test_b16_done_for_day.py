"""B16 reopen — done_for_day is NOT terminal + three fake-broker tests.

B16 mapped done_for_day to CANCELLED. That is wrong (Alpaca order
lifecycle docs, updated 2026-11-24): done_for_day = "The order is
done executing for the day, and will not receive further updates
until the next trading day." It is NOT terminal — unlike canceled /
expired / filled, which all say "no further updates will occur". A
done_for_day order can still be canceled, can still fill the next
trading day, and still counts against buying power until executed
or canceled.

The correction:
(1) done_for_day gets its own intent state DONE_FOR_DAY, separate
    from CANCELLED. Only a confirmed expired/canceled releases the
    unfilled remainder; the mirror never clears a DONE_FOR_DAY
    intent's expected position.
(2) The outstanding reservation is preserved: B15 sizing counts a
    DONE_FOR_DAY intent's unfilled qty from the intent store (the
    broker's OPEN query excludes done_for_day orders), with the same
    price chain (limit -> filled average -> last price -> entry
    price). An order the broker still lists as open is counted once
    (via the broker list); the merge dedupes by broker order id.
(3) No replacement while the original is unresolved: a DONE_FOR_DAY
    intent blocks its symbol (like UNKNOWN). The startup reconcile
    looks it up every run — filled adopts to FILLED, expired/canceled
    adopts to CANCELLED (releasing the remainder and allowing a
    replacement), still done_for_day / inconclusive keeps waiting.
(4) Trailing-stop coordination (test 3): the exit cancels the
    trailing stop first, fail-closed; if the exit then fails to
    submit, the shares are unprotected and a CRITICAL alert fires.

Tests (fake clients/fixtures only; e2e through main.py as a
subprocess, same harness as B13/B15/B16):
(1) Overnight resumption: a 2-share buy fills 1, becomes
    done_for_day, then fills the 2nd share after restart — one share
    stays reserved overnight, the final balance is 2 held / 0
    reserved, with exactly ONE submission attempt and no replacement
    while the original was unresolved. (1b) the reservation bites:
    with AMD in an uptrend, AMD is sized to 8 shares (572.88) — the
    same as B15's open-order case — not the 13 shares (930.93) it
    gets with no reservation.
(2) Fill during cancellation: the 2nd share fills between the cancel
    request and the broker's confirmation — the exit sells the final
    confirmed 2 shares, not the pre-cancel 1, and the expected
    balance matches the confirmed fills.
(3) Trailing stop + strategy exit: (3a) the exit fires first, then
    the stop — the stop is cancelled, total sold is 2, never more
    than the position; (3b) the stop fires first, then the exit —
    the exit sees 0 held and submits no sell, total sold is 2 (all
    from the stop); (3c) the exit fails to submit — the stop is
    cancelled, the shares are unprotected, a CRITICAL alert fires,
    the entry stays open, and the next run does NOT re-submit the
    exit (the UNKNOWN exit intent blocks the symbol).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

from test_b13_soak_readiness import (  # noqa: E402
    _guard_timing, _latest_decision_log, _read_alerts, _read_intents, _sessions,
)
from test_b15_sizing_reservations import (  # noqa: E402
    AMD_UPTREND, NVDA_FALLING, _broker_state, _env, _nvda_limit,
    _open_order_coid, _order_for, _run_main, _write_fixture,
)
from test_b16_cancel_expiry import (  # noqa: E402
    _StubLeg, _intent_by_id, _order_count, _reset_submitted_today,
    _run_buy, _strategy_state, _working_buy,
)
from fake_dual_broker import FileBackedFakeBroker  # noqa: E402

VENV_PY = REPO / ".venv" / "bin" / "python"


# ---------------------------------------------------------------- helpers

def _alerts_log(home: Path) -> str:
    path = home / "alerts.log"
    return path.read_text() if path.exists() else ""


def _stops(fixture_dir: Path) -> dict:
    return _broker_state(fixture_dir).get("stops", {})


def _stop_coid_for(fixture_dir: Path, symbol: str) -> str:
    stops = [coid for coid, o in _stops(fixture_dir).items()
             if o.get("symbol") == symbol]
    assert len(stops) == 1, _stops(fixture_dir)
    return stops[0]


def _total_sold(fixture_dir: Path, symbol: str) -> float:
    """Shares actually sold: filled sell qty across strategy orders
    AND trailing stops. The invariant in test 3 is that this never
    exceeds the position (2)."""
    state = _broker_state(fixture_dir)
    total = 0.0
    for orders in (state.get("orders", {}), state.get("stops", {})):
        for o in orders.values():
            if o.get("symbol") == symbol and str(o.get("side", "")).lower() == "sell":
                total += float(o.get("filled_qty") or 0.0)
    return total


def _held(fixture_dir: Path, symbol: str) -> float:
    """Net filled qty for a symbol (buys + / sells -)."""
    state = _broker_state(fixture_dir)
    total = 0.0
    for orders in (state.get("orders", {}), state.get("stops", {})):
        for o in orders.values():
            if o.get("symbol") == symbol:
                qty = float(o.get("filled_qty") or 0.0)
                total += qty if str(o.get("side", "")).lower() == "buy" else -qty
    return total


# ---------------------------------------------------------------- unit tests:
# the done_for_day state machine

def test_normalize_done_for_day_is_own_state():
    """_normalize_broker_status lowercases (tolerating the SDK enum
    repr); _adopt_broker_order maps the normalized value to the
    intent state — done_for_day to DONE_FOR_DAY, expired/canceled
    to CANCELLED. The repr case is pinned end-to-end below."""
    from order_manager import _normalize_broker_status

    assert _normalize_broker_status("done_for_day") == "done_for_day"
    assert _normalize_broker_status("OrderStatus.DONE_FOR_DAY") == "done_for_day"
    assert _normalize_broker_status("DONE_FOR_DAY") == "done_for_day"
    assert _normalize_broker_status("OrderStatus.EXPIRED") == "expired"
    assert _normalize_broker_status("expired") == "expired"
    assert _normalize_broker_status("filled") == "filled"
    assert _normalize_broker_status("rejected") == "rejected"
    assert _normalize_broker_status("new") == "new"
    assert _normalize_broker_status("partially_filled") == "partially_filled"


def test_adopt_sdk_enum_repr_done_for_day(tmp_path):
    """str(OrderStatus.DONE_FOR_DAY) is 'OrderStatus.DONE_FOR_DAY' —
    the SDK path must map it to DONE_FOR_DAY, not CANCELLED and not
    SUBMITTED (the B16 bug, in its repr form)."""
    from order_manager import OrderManager

    manager = OrderManager(str(tmp_path / "intents.db"))
    intent = _submit_intent(manager)
    adopted = manager.reconcile_open_intents(
        lookup_fn=lambda coid: {"id": "fake-0001",
                                "status": "OrderStatus.DONE_FOR_DAY",
                                "filled_qty": 1.0})
    assert adopted[0]["status"] == "DONE_FOR_DAY"
    assert manager.is_blocked("NVDA", account="paper")


def _submit_intent(manager, *, symbol="NVDA", inputs_hash="abc"):
    from order_manager import build_intent

    intent = build_intent(design_id="live-trend", trading_day="2026-11-25",
                     symbol=symbol, side="buy", qty=2.0,
                     order_type="limit", limit_price=167.25,
                     inputs_hash=inputs_hash, account="paper")
    manager.submit(intent, place_order_fn=lambda o: {
        "id": "fake-0001", "status": "submitted",
        "broker_order_id": "fake-0001", "submitted": o},
        lookup_fn=lambda coid: {"id": "fake-0001", "status": "submitted",
                                "filled_qty": 0.0})
    return intent


def _second_intent(*, inputs_hash="def"):
    from order_manager import build_intent

    return build_intent(design_id="live-trend", trading_day="2026-11-26",
                   symbol="NVDA", side="buy", qty=2.0,
                   order_type="limit", limit_price=167.25,
                   inputs_hash=inputs_hash, account="paper")


def test_done_for_day_blocks_replacement_until_resolved(tmp_path):
    """(3) A DONE_FOR_DAY intent blocks its symbol like UNKNOWN: a
    second intent for the same symbol is refused (BLOCKED, no place
    call). A confirmed expired releases the remainder — the block
    clears and the same second intent can then be submitted."""
    from order_manager import OrderManager

    manager = OrderManager(str(tmp_path / "intents.db"))
    intent = _submit_intent(manager)
    adopted = manager.reconcile_open_intents(
        lookup_fn=lambda coid: {"id": "fake-0001", "status": "done_for_day",
                                "filled_qty": 1.0})
    assert adopted[0]["status"] == "DONE_FOR_DAY"
    assert adopted[0]["filled_qty"] == 1.0
    assert manager.is_blocked("NVDA", account="paper")
    assert manager.blocked_symbols() == ["NVDA"]
    assert len(manager.done_for_day_intents("NVDA")) == 1

    second = _second_intent()
    placed = []
    got = manager.submit(second, place_order_fn=lambda o: placed.append(o) or {
        "id": "fake-0002", "status": "submitted", "broker_order_id": "fake-0002"},
        lookup_fn=lambda coid: None)
    assert got["status"] == "BLOCKED"
    assert placed == []

    # A confirmed expired releases the unfilled remainder.
    manager.reconcile_open_intents(
        lookup_fn=lambda coid: {"id": "fake-0001", "status": "expired",
                                "filled_qty": 1.0})
    assert manager.get_intent(intent["intent_id"])["status"] == "CANCELLED"
    assert not manager.is_blocked("NVDA", account="paper")
    assert manager.done_for_day_intents("NVDA") == []
    got = manager.submit(second, place_order_fn=lambda o: placed.append(o) or {
        "id": "fake-0002", "status": "submitted", "broker_order_id": "fake-0002"},
        lookup_fn=lambda coid: {"id": "fake-0002", "status": "submitted",
                                "filled_qty": 0.0})
    assert got["status"] == "SUBMITTED"
    assert len(placed) == 1


def test_done_for_day_resolves_to_filled(tmp_path):
    """(3) A lookup that finds the order filled overnight adopts
    FILLED — the reservation is released for the filled part and the
    block clears."""
    from order_manager import OrderManager

    manager = OrderManager(str(tmp_path / "intents.db"))
    intent = _submit_intent(manager)
    manager.reconcile_open_intents(
        lookup_fn=lambda coid: {"id": "fake-0001", "status": "done_for_day",
                                "filled_qty": 1.0})
    assert manager.get_intent(intent["intent_id"])["status"] == "DONE_FOR_DAY"
    manager.reconcile_open_intents(
        lookup_fn=lambda coid: {"id": "fake-0001", "status": "filled",
                                "filled_qty": 2.0})
    assert manager.get_intent(intent["intent_id"])["status"] == "FILLED"
    assert manager.get_intent(intent["intent_id"])["filled_qty"] == 2.0
    assert not manager.is_blocked("NVDA", account="paper")


def test_resubmitting_done_for_day_intent_reconciles_never_resubmits(tmp_path):
    """(3) Re-submitting the SAME intent while it is DONE_FOR_DAY
    goes to lookup-only reconcile: a lookup that says it is still
    done_for_day returns the intent unchanged — attempts still 1, no
    second place call."""
    from order_manager import OrderManager

    manager = OrderManager(str(tmp_path / "intents.db"))
    intent = _submit_intent(manager)
    manager.reconcile_open_intents(
        lookup_fn=lambda coid: {"id": "fake-0001", "status": "done_for_day",
                                "filled_qty": 1.0})
    placed = []
    got = manager.submit(intent, place_order_fn=lambda o: placed.append(o) or {
        "id": "fake-0001", "status": "submitted", "broker_order_id": "fake-0001"},
        lookup_fn=lambda coid: {"id": "fake-0001", "status": "done_for_day",
                                "filled_qty": 1.0})
    assert got["status"] == "DONE_FOR_DAY"
    assert got["attempts"] == 1
    assert placed == []


def test_done_for_day_inconclusive_lookup_stays_no_alert(tmp_path):
    """(3) An inconclusive lookup (None, then raising) leaves a
    DONE_FOR_DAY intent DONE_FOR_DAY — no UNKNOWN escalation, no
    alert, no block change: the order was confirmed accepted, so
    there is nothing ambiguous to resolve."""
    from order_manager import OrderManager

    manager = OrderManager(str(tmp_path / "intents.db"))
    intent = _submit_intent(manager)
    manager.reconcile_open_intents(
        lookup_fn=lambda coid: {"id": "fake-0001", "status": "done_for_day",
                                "filled_qty": 1.0})
    assert manager.reconcile_open_intents(lookup_fn=lambda coid: None) == []
    assert manager.get_intent(intent["intent_id"])["status"] == "DONE_FOR_DAY"
    assert manager.reconcile_open_intents(
        lookup_fn=lambda coid: (_ for _ in ()).throw(RuntimeError("boom"))) == []
    assert manager.get_intent(intent["intent_id"])["status"] == "DONE_FOR_DAY"
    assert manager.get_alerts() == []
    assert manager.is_blocked("NVDA", account="paper")


def test_reconcile_on_done_for_day_inconclusive_stays(tmp_path):
    """Same as above, through reconcile() directly (the path submit()
    takes for a DONE_FOR_DAY intent): inconclusive lookup, no alert,
    no UNKNOWN escalation."""
    from order_manager import OrderManager

    manager = OrderManager(str(tmp_path / "intents.db"))
    intent = _submit_intent(manager)
    manager.reconcile_open_intents(
        lookup_fn=lambda coid: {"id": "fake-0001", "status": "done_for_day",
                                "filled_qty": 1.0})
    current = manager.get_intent(intent["intent_id"])
    out = manager.reconcile(current, lookup_fn=lambda coid: None,
                            grace_seconds=0.0, max_polls=1,
                            sleep_fn=lambda s: None)
    assert out["status"] == "DONE_FOR_DAY"
    assert manager.get_alerts() == []


def test_mirror_skips_done_for_day(tmp_path):
    """(1) The mirror must NOT clear a DONE_FOR_DAY intent's expected
    position — only CANCELLED intents clear. A DONE_FOR_DAY intent
    leaves the entry and its expected qty untouched."""
    from order_manager import OrderManager
    from strategy_runner import StrategyState, _mirror_cancelled_intents_into_state

    state = StrategyState()
    state.open_entries["NVDA"] = {"symbol": "NVDA", "qty": 2.0,
                                  "intent_id": "intent-1",
                                  "entry_price": 167.25}
    state.expected_positions["paper"]["NVDA"] = {"qty": 2.0, "decision_id": "d1"}
    adjustments = _mirror_cancelled_intents_into_state(state, [
        {"symbol": "NVDA", "intent_id": "intent-1",
         "filled_qty": 1.0, "status": "DONE_FOR_DAY"}])
    assert adjustments == []
    assert state.open_entries["NVDA"]["qty"] == 2.0
    assert state.expected_positions["paper"]["NVDA"]["qty"] == 2.0
    adjustments = _mirror_cancelled_intents_into_state(state, [
        {"symbol": "NVDA", "intent_id": "intent-1",
         "filled_qty": 1.0, "status": "CANCELLED"}])
    assert adjustments == [{"symbol": "NVDA", "intent_id": "intent-1",
                            "filled_qty": 1.0, "action": "reduced_to_filled"}]
    assert state.open_entries["NVDA"]["qty"] == 1.0


# ---------------------------------------------------------------- unit tests:
# the reservation is preserved (and never double-counted)

def test_done_for_day_reservation_from_intent_store(tmp_path):
    """(2) A DONE_FOR_DAY buy is invisible to the broker's OPEN query,
    so the sizing reservation comes from the intent store: 1 remaining
    share at its limit (167.25). The filled share is not reserved
    again — it is counted once, in the filled position."""
    from order_manager import OrderManager
    from reconcile import _open_orders_including_done_for_day
    from strategy_runner import _open_buy_reservations

    manager = OrderManager(str(tmp_path / "intents.db"))
    _submit_intent(manager)
    manager.reconcile_open_intents(
        lookup_fn=lambda coid: {"id": "fake-0001", "status": "done_for_day",
                                "filled_qty": 1.0, "limit_price": 167.25})

    class _Leg:
        def open_orders(self):
            return []  # the broker's OPEN query excludes done_for_day

    merged = _open_orders_including_done_for_day(
        _Leg(), manager,
        lookup_fn=lambda coid: {"id": "fake-0001", "status": "done_for_day",
                                "filled_qty": 1.0, "limit_price": 167.25})
    assert len(merged) == 1
    assert merged[0]["status"] == "done_for_day"
    total, detail = _open_buy_reservations(merged, {"NVDA"}, {}, {"NVDA": 167.26})
    assert total == 167.25
    assert detail == [{"symbol": "NVDA", "order_id": "fake-0001",
                       "remaining_qty": 1.0, "price": 167.25,
                       "price_source": "limit_price", "notional": 167.25}]


def test_done_for_day_reservation_no_double_count(tmp_path):
    """(2) The no-double-count rule: an order the broker still lists
    as open is counted ONCE (via the broker list), even when the
    intent store also holds it as DONE_FOR_DAY — the merge dedupes
    by broker order id. Without the dedupe the reservation would be
    2 x 167.25 = 334.50."""
    from order_manager import OrderManager
    from reconcile import _open_orders_including_done_for_day
    from strategy_runner import _open_buy_reservations

    manager = OrderManager(str(tmp_path / "intents.db"))
    _submit_intent(manager)
    manager.reconcile_open_intents(
        lookup_fn=lambda coid: {"id": "fake-0001", "status": "done_for_day",
                                "filled_qty": 1.0, "limit_price": 167.25})

    class _Leg:
        def open_orders(self):
            # Contradictory state (the OPEN query should exclude a
            # done_for_day order), but the merge must still count it
            # once — the broker list is fresher and wins the dedupe.
            return [{"id": "fake-0001", "symbol": "NVDA", "side": "buy",
                     "qty": 2.0, "filled_qty": 1.0, "limit_price": 167.25,
                     "status": "submitted"}]

    merged = _open_orders_including_done_for_day(
        _Leg(), manager,
        lookup_fn=lambda coid: {"id": "fake-0001", "status": "done_for_day",
                                "filled_qty": 1.0, "limit_price": 167.25})
    assert len(merged) == 1
    assert merged[0]["status"] == "submitted"  # the broker list wins
    total, _ = _open_buy_reservations(merged, {"NVDA"}, {}, {"NVDA": 167.26})
    assert total == 167.25


def test_done_for_day_orders_scoped_by_account(tmp_path):
    """A paper DONE_FOR_DAY intent must never be counted as a
    live-leg holding: the live leg's reconcile would otherwise see
    an unexpected position for an order it never placed (the bug
    this test caught — the live leg counted the paper remainder)."""
    from order_manager import OrderManager
    from reconcile import _done_for_day_orders, _open_orders_including_done_for_day

    manager = OrderManager(str(tmp_path / "intents.db"))
    _submit_intent(manager)
    manager.reconcile_open_intents(
        lookup_fn=lambda coid: {"id": "fake-0001", "status": "done_for_day",
                                "filled_qty": 1.0, "limit_price": 167.25})

    class _Leg:
        def open_orders(self):
            return []

    lookup = lambda coid: {"id": "fake-0001", "status": "done_for_day",
                           "filled_qty": 1.0, "limit_price": 167.25}
    assert len(_done_for_day_orders(manager, lookup, account="paper")) == 1
    assert _done_for_day_orders(manager, lookup, account="live") == []
    assert len(_open_orders_including_done_for_day(
        _Leg(), manager, lookup, account="paper")) == 1
    assert _open_orders_including_done_for_day(
        _Leg(), manager, lookup, account="live") == []


def test_done_for_day_reservation_price_fallbacks(tmp_path):
    """(2) The intent-derived order uses the same price chain: a
    market buy (no limit) prices its remainder from the filled
    average; with neither, from the last price; with neither of those,
    from the entry price."""
    from order_manager import OrderManager, build_intent
    from reconcile import _open_orders_including_done_for_day
    from strategy_runner import _open_buy_reservations

    manager = OrderManager(str(tmp_path / "intents.db"))
    intent = build_intent(design_id="live-trend", trading_day="2026-11-25",
                     symbol="NVDA", side="buy", qty=2.0,
                     order_type="market", limit_price=None,
                     inputs_hash="mkt", account="paper")
    manager.submit(intent, place_order_fn=lambda o: {
        "id": "fake-0001", "status": "submitted",
        "broker_order_id": "fake-0001", "submitted": o},
        lookup_fn=lambda coid: {"id": "fake-0001", "status": "submitted",
                                "filled_qty": 0.0})
    manager.reconcile_open_intents(
        lookup_fn=lambda coid: {"id": "fake-0001", "status": "done_for_day",
                                "filled_qty": 1.0, "filled_avg_price": 166.50})

    class _Leg:
        def open_orders(self):
            return []

    merged = _open_orders_including_done_for_day(
        _Leg(), manager,
        lookup_fn=lambda coid: {"id": "fake-0001", "status": "done_for_day",
                                "filled_qty": 1.0, "filled_avg_price": 166.50})
    total, detail = _open_buy_reservations(merged, {"NVDA"}, {"NVDA": 167.26}, {})
    assert total == 166.50
    assert detail[0]["price_source"] == "filled_avg_price"


# ---------------------------------------------------------------- unit tests:
# the fake broker supports done_for_day + fill-during-cancel

def test_fake_broker_done_for_day_fill_and_cancel(tmp_path):
    from fake_broker import FakeBroker

    broker = FakeBroker()
    broker.submit({"symbol": "NVDA", "side": "buy", "qty": 2.0,
                   "order_type": "limit", "limit_price": 167.25,
                   "client_order_id": "coid-dfd"})
    broker.apply_partial_fill("coid-dfd", "fill-1", 1, 167.25)
    marked = broker.done_for_day("coid-dfd")
    assert marked["status"] == "done_for_day"
    assert marked["filled_qty"] == 1.0
    # A fill the next day is ACCEPTED (done_for_day is not terminal)
    # and moves the order back to partially_filled, then filled.
    fill = broker.apply_partial_fill("coid-dfd", "fill-2", 1, 167.25)
    assert fill["qty"] == 1.0
    order = broker.get_by_client_order_id("coid-dfd")
    assert order["status"] == "filled" and order["filled_qty"] == 2.0
    # A done_for_day order can still be cancelled (Alpaca: an order
    # may be canceled up until filled / canceled / expired).
    broker2 = FakeBroker()
    broker2.submit({"symbol": "NVDA", "side": "buy", "qty": 2.0,
                    "order_type": "limit", "limit_price": 167.25,
                    "client_order_id": "coid-dfd2"})
    broker2.apply_partial_fill("coid-dfd2", "fill-1", 1, 167.25)
    broker2.done_for_day("coid-dfd2")
    cancelled = broker2.cancel("coid-dfd2")
    assert cancelled["status"] == "cancelled"
    assert cancelled["filled_qty"] == 1.0
    # Marking a filled order done_for_day is a no-op; expire() (the
    # terminal path) is unchanged.
    assert broker.done_for_day("coid-dfd")["status"] == "filled"
    broker3 = FakeBroker()
    broker3.submit({"symbol": "NVDA", "side": "buy", "qty": 2.0,
                    "order_type": "limit", "limit_price": 167.25,
                    "client_order_id": "coid-exp"})
    assert broker3.expire("coid-exp")["status"] == "expired"


def test_fake_broker_fill_during_cancel_hook(tmp_path):
    """The fill lands BETWEEN the cancel request and the broker's
    confirmation: the cancel result reports the FINAL filled qty
    (2), not the pre-cancel snapshot (1). Consumed once."""
    from fake_broker import FakeBroker

    broker = FakeBroker()
    broker.submit({"symbol": "NVDA", "side": "buy", "qty": 2.0,
                   "order_type": "limit", "limit_price": 167.25,
                   "client_order_id": "coid-race"})
    broker.apply_partial_fill("coid-race", "fill-1", 1, 167.25)
    broker.set_fill_during_cancel("coid-race", 1, 167.25)
    cancelled = broker.cancel("coid-race")
    # The hook fill COMPLETED the order (2 of 2) before the cancel
    # landed, so the broker's correct answer is "filled", not
    # "cancelled" — a filled order cannot be cancelled. The point is
    # the cancel result reports the FINAL filled qty (2), not the
    # pre-cancel snapshot (1).
    assert cancelled["status"] == "filled"
    assert cancelled["filled_qty"] == 2.0
    assert broker.get_by_client_order_id("coid-race")["filled_qty"] == 2.0


def test_fake_broker_trailing_stop_visible_not_counted(tmp_path):
    """A trailing stop is a real order (visible in open_orders with
    order_type trailing_stop) but is NOT a strategy submission:
    order_count() and list_orders() exclude it."""
    from fake_broker import FakeBroker

    broker = FakeBroker()
    broker.submit({"symbol": "NVDA", "side": "buy", "qty": 2.0,
                   "order_type": "limit", "limit_price": 167.25,
                   "client_order_id": "coid-buy"})
    stop = broker.attach_trailing_stop("NVDA", 2.0, 0.05)
    assert stop["id"] == "fake-trail-0001"
    assert stop["client_order_id"] == "trail-NVDA-0001"
    assert broker.order_count() == 1
    assert len(broker.list_orders()) == 1
    # The stop is a real order in the store, listed by list_stops().
    assert len(broker.list_stops()) == 1
    assert broker.list_stops()[0]["order_type"] == "trailing_stop"
    # Trigger with nothing held fills zero and stays working.
    broker2 = FakeBroker()
    stop2 = broker2.attach_trailing_stop("NVDA", 2.0, 0.05)
    noop = broker2.trigger_trailing_stop(stop2["client_order_id"], 167.25)
    assert noop["qty"] == 0.0
    assert broker2.get_by_client_order_id(
        stop2["client_order_id"])["status"] == "submitted"


# ---------------------------------------------------------------- unit tests:
# the cancel helper cancels an invisible done_for_day order by
# client_order_id, and the stop-cancel helper

def test_cancel_helper_cancels_invisible_done_for_day_order(tmp_path):
    """The entry's order is done_for_day — invisible to open_orders()
    — but still working. The helper cancels it by client_order_id
    (== intent_id); the tripwire lookup confirms 'cancelled' and the
    exit may go, with the filled qty from the cancel result."""
    from order_manager import OrderManager
    from strategy_runner import _cancel_working_orders_on_leg

    manager = OrderManager(str(tmp_path / "intents.db"))
    intent = _submit_intent(manager)
    manager.reconcile_open_intents(
        lookup_fn=lambda coid: {"id": "fake-0001", "status": "done_for_day",
                                "filled_qty": 1.0})
    # The stub leg mirrors the broker rule: done_for_day orders are
    # excluded from open_orders(), but cancel by client_order_id
    # still finds them.
    leg = _StubLeg([{"id": "fake-0001", "client_order_id": intent["intent_id"],
                     "symbol": "NVDA", "side": "buy", "qty": 2.0,
                     "filled_qty": 1.0, "status": "done_for_day"}],
                   positions={"NVDA": {"qty": 1.0}})
    assert leg.open_orders() == []  # invisible, as the broker shows it
    ok, filled, reason = _cancel_working_orders_on_leg(
        leg, "NVDA", "buy", manager=manager, intent_id=intent["intent_id"],
        lookup_fn=lambda coid: {"id": "fake-0001", "status": "cancelled",
                                "filled_qty": 1.0})
    assert ok and filled == 1.0 and reason == "", reason
    assert leg.cancel_calls == [(None, intent["intent_id"])]
    assert manager.get_intent(intent["intent_id"])["status"] == "CANCELLED"


def test_stop_cancel_helper(tmp_path):
    """_cancel_trailing_stop_on_leg: cancels an open stop (ok), is a
    no-op when there is no stop (ok), and fails closed when the
    cancel does not take effect."""
    from fake_dual_broker import FakeDualBroker
    from strategy_runner import _cancel_trailing_stop_on_leg

    fixture = tmp_path / "fixture"
    _write_fixture(fixture)
    broker = FakeDualBroker(fixture)
    # No stop yet: nothing to cancel -> ok.
    ok, reason = _cancel_trailing_stop_on_leg(broker.paper, "NVDA")
    assert ok and reason == ""
    stop = broker.paper.attach_trailing_stop("NVDA", 2.0, 0.05)
    assert stop["id"] == "fake-trail-0001"
    open_orders = broker.paper.open_orders()
    stops = [o for o in open_orders if o.get("order_type") == "trailing_stop"]
    assert len(stops) == 1 and stops[0]["qty"] == 2.0
    ok, reason = _cancel_trailing_stop_on_leg(broker.paper, "NVDA")
    assert ok and reason == "", reason
    assert broker.paper.open_orders() == []
    assert _stops(fixture)[stop["client_order_id"]]["status"] == "cancelled"

    # A stop whose cancel does not take effect -> not ok (the exit
    # must not proceed while the stop may still fire).
    class _NoopCancelLeg:
        def open_orders(self):
            return [{"id": "fake-trail-0001", "client_order_id": "trail-NVDA-0001",
                     "symbol": "NVDA", "side": "sell", "qty": 2.0,
                     "filled_qty": 0.0, "order_type": "trailing_stop",
                     "status": "submitted"}]

        def cancel_order(self, order_id=None, client_order_id=None):
            return {"id": order_id, "status": "submitted", "filled_qty": 0.0}

    ok, reason = _cancel_trailing_stop_on_leg(_NoopCancelLeg(), "NVDA")
    assert not ok and "did not take effect" in reason


def test_stop_trigger_capped_at_held(tmp_path):
    """A trailing stop must never sell more than the position (the
    broker-level backstop): with 1 share held it fills 1 of 2, and
    with 0 held it fills 0 and stays working."""
    from fake_broker import FakeBroker

    broker = FakeBroker()
    broker.submit({"symbol": "NVDA", "side": "buy", "qty": 2.0,
                   "order_type": "limit", "limit_price": 167.25,
                   "client_order_id": "coid-buy"})
    broker.apply_partial_fill("coid-buy", "fill-1", 1, 167.25)
    stop = broker.attach_trailing_stop("NVDA", 2.0, 0.05)
    fill = broker.trigger_trailing_stop(stop["client_order_id"], 167.25)
    assert fill["qty"] == 1.0
    assert broker.get_by_client_order_id(
        stop["client_order_id"])["filled_qty"] == 1.0
    # Nothing held: zero fill, still working.
    broker2 = FakeBroker()
    stop2 = broker2.attach_trailing_stop("NVDA", 2.0, 0.05)
    noop = broker2.trigger_trailing_stop(stop2["client_order_id"], 167.25)
    assert noop["qty"] == 0.0
    assert broker2.get_by_client_order_id(
        stop2["client_order_id"])["status"] == "submitted"


def test_sync_trailing_stops_after_mirror(tmp_path):
    """The startup stop sync: an entry removed by the mirror (0
    filled) has its stop cancelled; an entry reduced to its filled
    qty has its stop cancelled and re-attached for the filled qty."""
    from fake_dual_broker import FakeDualBroker
    from strategy_runner import StrategyState, _sync_trailing_stops_after_mirror

    fixture = tmp_path / "fixture"
    _write_fixture(fixture)
    broker = FakeDualBroker(fixture)
    guardian = SimpleNamespace(
        alerter=SimpleNamespace(send=lambda level, msg: None))
    state = StrategyState()

    # Removed (0 filled): the stop is cancelled, no re-attach.
    stop = broker.paper.attach_trailing_stop("NVDA", 2.0, 0.05)
    state.open_entries["NVDA"] = {"symbol": "NVDA", "qty": 2.0,
                                  "trail_percent": 0.05,
                                  "trail_stop_id": stop["id"]}
    synced = _sync_trailing_stops_after_mirror(
        broker, guardian, state,
        [{"symbol": "NVDA", "intent_id": "i1", "filled_qty": 0.0,
          "action": "removed"}])
    assert synced == [{"symbol": "NVDA", "action": "stop_cancelled",
                       "reason": "entry removed (0 filled)"}]
    assert _stops(fixture)[stop["client_order_id"]]["status"] == "cancelled"
    assert broker.paper.open_orders() == []

    # Reduced to filled (1 of 2): the old stop is cancelled and a new
    # one is attached for the filled qty with the same percent.
    stop2 = broker.paper.attach_trailing_stop("NVDA", 2.0, 0.05)
    state.open_entries["NVDA"] = {"symbol": "NVDA", "qty": 2.0,
                                  "trail_percent": 0.05,
                                  "trail_stop_id": stop2["id"]}
    synced = _sync_trailing_stops_after_mirror(
        broker, guardian, state,
        [{"symbol": "NVDA", "intent_id": "i1", "filled_qty": 1.0,
          "action": "reduced_to_filled"}])
    assert synced == [{"symbol": "NVDA", "action": "stop_reattached",
                       "reason": "filled_qty 1.0"}]
    assert _stops(fixture)[stop2["client_order_id"]]["status"] == "cancelled"
    new_stops = [o for o in _stops(fixture).values()
                 if o["status"] == "submitted"]
    assert len(new_stops) == 1 and new_stops[0]["qty"] == 1.0
    assert state.open_entries["NVDA"]["trail_stop_id"] == new_stops[0]["id"]


def test_sync_trailing_stops_after_mirror_alerts_on_failure(tmp_path):
    """A stop that cannot be cancelled after the mirror alerts
    CRITICAL (unprotected shares) instead of silently leaving an
    orphaned stop that could short-sell."""
    from strategy_runner import StrategyState, _sync_trailing_stops_after_mirror

    sent = []

    class _FailingLeg:
        def open_orders(self):
            return [{"id": "fake-trail-0001", "client_order_id": "trail-NVDA-0001",
                     "symbol": "NVDA", "side": "sell", "qty": 2.0,
                     "filled_qty": 0.0, "order_type": "trailing_stop",
                     "status": "submitted"}]

        def cancel_order(self, order_id=None, client_order_id=None):
            raise RuntimeError("broker 500")

    broker = SimpleNamespace(paper=_FailingLeg())
    guardian = SimpleNamespace(
        alerter=SimpleNamespace(send=lambda level, msg: sent.append((level, msg))))
    state = StrategyState()
    state.open_entries["NVDA"] = {"symbol": "NVDA", "qty": 2.0,
                                  "trail_percent": 0.05,
                                  "trail_stop_id": "fake-trail-0001"}
    synced = _sync_trailing_stops_after_mirror(
        broker, guardian, state,
        [{"symbol": "NVDA", "intent_id": "i1", "filled_qty": 0.0,
          "action": "removed"}])
    assert synced == [{"symbol": "NVDA", "action": "stop_cancel_failed",
                       "reason": "trailing-stop cancel failed for "
                                 "fake-trail-0001: broker 500"}]
    assert len(sent) == 1 and sent[0][0] == "critical"
    assert "NVDA" in sent[0][1]


# ---------------------------------------------------------------- e2e

def test_e2e_overnight_resumption(tmp_path):
    """(1) Run 1 buys NVDA qty 2 (limit, unfilled), fills 1, becomes
    done_for_day overnight. Run 2 (restart): status ok (no drift
    halt — the reconcile counts the done_for_day remainder from the
    intent store), one share stays reserved (open_buy_reservations
    1 x limit 167.25), the intent is still DONE_FOR_DAY, no
    replacement order was submitted (order count 1, attempts 1).
    Then the 2nd share fills. Run 3: 2 held, 0 reserved, the intent
    is FILLED, still exactly one submission attempt."""
    _guard_timing()
    home = tmp_path / "home"
    fixture = tmp_path / "fixture"
    home.mkdir(parents=True)
    _, coid, limit = _run_buy(home, fixture)

    from fake_dual_broker import FileBackedFakeBroker
    store = FileBackedFakeBroker(fixture)
    store.fill(coid, "fill-1", 1, limit)
    store.done_for_day(coid)
    assert _broker_state(fixture)["orders"][coid]["status"] == "done_for_day"
    assert _broker_state(fixture)["orders"][coid]["filled_qty"] == 1.0

    _reset_submitted_today(home)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"])
    assert rc == 0, out
    log2 = _latest_decision_log(home)
    assert log2["status"] == "ok", out
    assert log2["orders"] == []
    assert log2["state_sync"] == []
    assert log2["done_for_day"] == [{
        "symbol": "NVDA", "intent_id": coid,
        "filled_qty": 1.0, "remaining_qty": 1.0}]
    assert _intent_by_id(home, coid)["status"] == "DONE_FOR_DAY"
    assert _intent_by_id(home, coid)["attempts"] == 1
    assert _order_count(fixture) == 1
    # One share stays reserved overnight: the filled share at last
    # close plus the remaining share at its limit.
    lasts = _write_fixture(fixture)  # rewrite only to read the last price
    budget = log2["budget"]
    assert budget["open_buy_reservations"] == round(1 * limit, 2), budget
    assert budget["existing_notional"] == round(1 * lasts["NVDA"] + 1 * limit, 2), budget
    assert budget["buy_reservations"] == [{
        "symbol": "NVDA", "order_id": "fake-0001", "remaining_qty": 1.0,
        "price": limit, "price_source": "limit_price",
        "notional": round(1 * limit, 2)}]
    state = _strategy_state(home)
    assert state["open_entries"]["NVDA"]["qty"] == 2.0
    assert state["expected_positions"]["paper"]["NVDA"]["qty"] == 2.0
    assert _held(fixture, "NVDA") == 1.0

    # The 2nd share fills the next day.
    FileBackedFakeBroker(fixture).fill(coid, "fill-2", 1, limit)
    assert _broker_state(fixture)["orders"][coid]["status"] == "filled"
    assert _broker_state(fixture)["orders"][coid]["filled_qty"] == 2.0

    _reset_submitted_today(home)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"])
    assert rc == 0, out
    log3 = _latest_decision_log(home)
    assert log3["status"] == "ok", out
    assert log3["orders"] == []
    assert log3["done_for_day"] == []
    assert _intent_by_id(home, coid)["status"] == "FILLED"
    assert _intent_by_id(home, coid)["attempts"] == 1
    assert _order_count(fixture) == 1  # still exactly one submission
    budget3 = log3["budget"]
    assert budget3["open_buy_reservations"] == 0.0
    assert budget3["existing_notional"] == round(2 * lasts["NVDA"], 2), budget3
    assert _held(fixture, "NVDA") == 2.0
    state3 = _strategy_state(home)
    assert state3["open_entries"]["NVDA"]["qty"] == 2.0


def test_e2e_done_for_day_reservation_sizes_next_buy(tmp_path):
    """(1b) Same setup as (1), but AMD is in an uptrend in Run 2:
    the DONE_FOR_DAY remainder still reserves 1 x limit, so AMD is
    sized to 8 shares (572.88) — the same as B15's open-order case —
    not the 13 shares (930.93) it gets with no reservation."""
    _guard_timing()
    home = tmp_path / "home"
    fixture = tmp_path / "fixture"
    home.mkdir(parents=True)
    _, coid, limit = _run_buy(home, fixture)

    from fake_dual_broker import FileBackedFakeBroker
    store = FileBackedFakeBroker(fixture)
    store.fill(coid, "fill-1", 1, limit)
    store.done_for_day(coid)

    _reset_submitted_today(home)
    lasts = _write_fixture(fixture, amd_cycle=AMD_UPTREND)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"])
    assert rc == 0, out
    log2 = _latest_decision_log(home)
    assert log2["status"] == "ok", out
    assert log2["done_for_day"] == [{
        "symbol": "NVDA", "intent_id": coid,
        "filled_qty": 1.0, "remaining_qty": 1.0}]
    budget = log2["budget"]
    assert budget["open_buy_reservations"] == round(1 * limit, 2), budget
    assert budget["existing_notional"] == round(1 * 167.26 + 1 * limit, 2), budget
    amd = _order_for(log2, "AMD")
    assert amd["order"]["qty"] == 8
    # The notional is 8 x the ask (last close + 0.01), derived from
    # the fixture — B15's 572.88 used an ask of 71.61; the fixture's
    # latest session has since shifted, so the ask here is 71.6092
    # and 8 x 71.6092 = 572.87. The qty (8, not 13) is the proof the
    # reservation bites.
    assert amd["intended_notional"] == round(8 * (lasts["AMD"] + 0.01), 2)
    assert amd["intended_notional"] < 930.93  # the no-reservation control
    assert _intent_by_id(home, coid)["status"] == "DONE_FOR_DAY"
    assert _order_count(fixture) == 2  # NVDA buy + AMD buy (no NVDA replacement)


def test_e2e_fill_during_cancellation(tmp_path):
    """(2) Run 1 buys NVDA qty 2, fills 1. The 2nd share is armed to
    fill between the cancel request and the broker's confirmation.
    Run 2 (signal lost): the exit cancels the buy — the hook fills
    the 2nd share mid-cancel, completing the order — and the exit
    sells the final confirmed 2 shares, not the pre-cancel 1. The
    expected balance matches the confirmed fills (entry closed, no
    expected position left)."""
    _guard_timing()
    home = tmp_path / "home"
    fixture = tmp_path / "fixture"
    home.mkdir(parents=True)
    _, coid, limit = _run_buy(home, fixture, exit_on_signal_loss=1)

    from fake_dual_broker import FileBackedFakeBroker
    store = FileBackedFakeBroker(fixture)
    store.fill(coid, "fill-1", 1, limit)
    # Arm the race: the 2nd share fills when the exit cancels the buy.
    store.set_fill_during_cancel(coid, 1, limit)

    _reset_submitted_today(home)
    _write_fixture(fixture, nvda_cycle=NVDA_FALLING)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"],
                        exit_on_signal_loss=1)
    assert rc == 0, out
    log2 = _latest_decision_log(home)
    assert log2["status"] == "ok", out
    orders = _broker_state(fixture)["orders"]
    assert len(orders) == 2
    buy, sell = sorted(orders.values(), key=lambda o: o["id"])
    # The mid-cancel fill completed the buy (2 of 2): the broker's
    # correct answer is "filled", and the cancel result reported the
    # final filled qty — the exit sells 2, not the pre-cancel 1.
    assert buy["side"] == "buy" and buy["status"] == "filled"
    assert buy["filled_qty"] == 2.0
    assert sell["side"] == "sell" and sell["qty"] == 2.0
    assert sell["status"] in {"submitted", "new"}
    assert _stops(fixture)[_stop_coid_for(fixture, "NVDA")]["status"] == "cancelled"
    intents = _read_intents(home)
    assert len(intents) == 2
    exit_intent = intents[1]
    assert exit_intent["side"] == "sell" and exit_intent["qty"] == 2.0
    assert exit_intent["status"] == "SUBMITTED"
    assert exit_intent["broker_order_id"] == "fake-0002"
    assert _strategy_state(home)["open_entries"] == {}
    assert _strategy_state(home)["expected_positions"]["paper"] == {}
    assert _held(fixture, "NVDA") == 2.0  # 2 bought, sell not yet filled


def test_e2e_exit_fires_first_then_stop(tmp_path):
    """(3a) The exit fires first: Run 1 buys 2, fills 2. Run 2
    (signal lost) cancels the trailing stop, then sells 2. Triggering
    the stop afterwards sells nothing — total sold stays 2, never
    more than the position."""
    _guard_timing()
    home = tmp_path / "home"
    fixture = tmp_path / "fixture"
    home.mkdir(parents=True)
    _, coid, limit = _run_buy(home, fixture, exit_on_signal_loss=1)

    from fake_dual_broker import FileBackedFakeBroker
    store = FileBackedFakeBroker(fixture)
    store.fill(coid, "fill-1", 2, limit)
    stop_coid = _stop_coid_for(fixture, "NVDA")
    assert _stops(fixture)[stop_coid]["status"] == "submitted"

    _reset_submitted_today(home)
    _write_fixture(fixture, nvda_cycle=NVDA_FALLING)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"],
                        exit_on_signal_loss=1)
    assert rc == 0, out
    log2 = _latest_decision_log(home)
    assert log2["status"] == "ok", out
    orders = _broker_state(fixture)["orders"]
    assert len(orders) == 2
    buy, sell = sorted(orders.values(), key=lambda o: o["id"])
    assert buy["filled_qty"] == 2.0
    assert sell["side"] == "sell" and sell["qty"] == 2.0
    assert sell["status"] in {"submitted", "new"}
    assert _stops(fixture)[stop_coid]["status"] == "cancelled"
    assert _strategy_state(home)["open_entries"] == {}
    # Fill the exit sell, then trigger the (cancelled) stop: it must
    # sell nothing — total sold stays 2, the position.
    store = FileBackedFakeBroker(fixture)  # fresh instance (see B16 note)
    store.fill(sell["client_order_id"], "fill-sell", 2, limit)
    assert _total_sold(fixture, "NVDA") == 2.0
    noop = store.trigger_trailing_stop(stop_coid, limit)
    assert noop["qty"] == 0.0
    assert _stops(fixture)[stop_coid]["filled_qty"] == 0.0
    assert _total_sold(fixture, "NVDA") == 2.0
    assert _held(fixture, "NVDA") == 0.0


def test_e2e_stop_fires_first_then_exit(tmp_path):
    """(3b) The stop fires first: Run 1 buys 2, fills 2; the stop
    sells both before Run 2. Run 2 (signal lost) sees 0 held and
    submits NO sell — the entry closes without a sale, total sold is
    2 (all from the stop), and no alert fires."""
    _guard_timing()
    home = tmp_path / "home"
    fixture = tmp_path / "fixture"
    home.mkdir(parents=True)
    _, coid, limit = _run_buy(home, fixture, exit_on_signal_loss=1)

    from fake_dual_broker import FileBackedFakeBroker
    store = FileBackedFakeBroker(fixture)
    store.fill(coid, "fill-1", 2, limit)
    stop_coid = _stop_coid_for(fixture, "NVDA")
    triggered = store.trigger_trailing_stop(stop_coid, limit)
    assert triggered["qty"] == 2.0
    assert _total_sold(fixture, "NVDA") == 2.0
    assert _held(fixture, "NVDA") == 0.0

    _reset_submitted_today(home)
    _write_fixture(fixture, nvda_cycle=NVDA_FALLING)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"],
                        exit_on_signal_loss=1)
    assert rc == 0, out
    log2 = _latest_decision_log(home)
    assert log2["status"] == "ok", out
    assert _order_count(fixture) == 1  # only the buy; no sell submitted
    assert _total_sold(fixture, "NVDA") == 2.0
    assert _strategy_state(home)["open_entries"] == {}
    assert "unprotected" not in _alerts_log(home)
    assert _read_alerts(home) == []


def test_e2e_exit_failure_leaves_unprotected_alert(tmp_path):
    """(3c) The exit fails to submit: Run 1 buys 2, fills 2. Run 2
    (signal lost) cancels the trailing stop, then the exit submit
    fails (injected broker failure, never accepted). The shares are
    unprotected: a CRITICAL alert fires (alerts.log + intent store),
    the entry stays open, no sell is submitted, and total sold is 0.
    Run 3 does NOT re-submit the exit — the UNKNOWN exit intent
    blocks the symbol (exact-once) — the entry stays open and the
    alert persists."""
    _guard_timing()
    home = tmp_path / "home"
    fixture = tmp_path / "fixture"
    home.mkdir(parents=True)
    _, coid, limit = _run_buy(home, fixture, exit_on_signal_loss=1)

    from fake_dual_broker import FileBackedFakeBroker
    store = FileBackedFakeBroker(fixture)
    store.fill(coid, "fill-1", 2, limit)
    stop_coid = _stop_coid_for(fixture, "NVDA")
    store.fail_next_submit()

    _reset_submitted_today(home)
    _write_fixture(fixture, nvda_cycle=NVDA_FALLING)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"],
                        exit_on_signal_loss=1)
    assert rc == 0, out
    log2 = _latest_decision_log(home)
    assert log2["status"] == "ok", out
    assert _order_count(fixture) == 1  # no sell submitted
    assert _total_sold(fixture, "NVDA") == 0.0
    assert _stops(fixture)[stop_coid]["status"] == "cancelled"
    state2 = _strategy_state(home)
    assert state2["open_entries"]["NVDA"]["qty"] == 2.0  # entry stays open
    assert state2["expected_positions"]["paper"]["NVDA"]["qty"] == 2.0
    intents = _read_intents(home)
    assert len(intents) == 2
    exit_intent = intents[1]
    assert exit_intent["side"] == "sell" and exit_intent["qty"] == 2.0
    assert exit_intent["status"] == "UNKNOWN"
    assert exit_intent["attempts"] == 1
    assert exit_intent["broker_order_id"] is None
    assert any(s["stage"] == "exit_unprotected" for s in log2["skipped"])
    alerts_text = _alerts_log(home)
    assert "CRITICAL" in alerts_text and "unprotected" in alerts_text
    assert "NVDA" in alerts_text
    # Two alerts for the exit intent: the manager's "lookup
    # inconclusive" (the order was never accepted) and the exit's
    # "unprotected" alert — both unresolved.
    stored_alerts = _read_alerts(home)
    assert len(stored_alerts) == 2
    assert {a["intent_id"] for a in stored_alerts} == {exit_intent["intent_id"]}
    assert any("unprotected" in a["reason"] for a in stored_alerts)
    assert all(a["resolved"] == 0 for a in stored_alerts)

    # Run 3: the exit is NOT re-submitted (the UNKNOWN exit intent
    # blocks NVDA — exact-once), the entry stays open, the alert
    # persists, and nothing was sold.
    _reset_submitted_today(home)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"],
                        exit_on_signal_loss=1)
    assert rc == 0, out
    log3 = _latest_decision_log(home)
    assert log3["status"] == "ok", out
    assert _order_count(fixture) == 1
    assert _total_sold(fixture, "NVDA") == 0.0
    assert _strategy_state(home)["open_entries"]["NVDA"]["qty"] == 2.0
    assert _intent_by_id(home, exit_intent["intent_id"])["status"] == "UNKNOWN"
    assert "unprotected" in _alerts_log(home)
    assert len(_read_alerts(home)) == 2
