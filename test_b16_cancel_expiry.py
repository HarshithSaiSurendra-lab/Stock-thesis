"""B16 — cancel/expiry state feedback + exit cancels working buy.

Acceptance tests (fake clients/fixtures only; e2e through main.py as
a subprocess with dated fixtures, like B13/B15):

(1) Unfilled DAY limit order expires overnight: Run 1 submits the buy
    (limit, unfilled); the broker marks it expired; Run 2 (next day)
    does NOT drift-halt, the stale expected position is cleared, and
    the symbol can be re-selected.
(2) Partial fill + cancel: 1 of 2 shares filled, remainder cancelled
    -> expected position becomes the filled qty only; reconcile
    passes; sizing counts the filled position only (no reservation
    for the cancelled remainder).
(3) Exit with a working buy: entry partially filled, buy still
    working, signal lost -> the buy is cancelled FIRST, the exit sells
    only the filled qty, and a later fill attempt on the cancelled
    order never re-opens the position.
(3b) Exit with an unfilled buy: nothing held -> no sell is submitted;
    the entry is closed without a sale.
(4) Regression: no open orders -> behaviour identical to before
    (startup sync empty, same orders as the B13/B15 e2e baseline).

Unit tests pin the pieces: the expired/done_for_day -> CANCELLED
mapping (incl. the SDK's str(OrderStatus.EXPIRED) repr),
reconcile_open_intents, the state mirror, the cancel-first helper,
AlpacaLeg.cancel_order (SDK + REST + 404), FakeLeg.cancel_order,
FakeBroker.expire, and the fill-after-cancel no-op.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_b13_soak_readiness import (  # noqa: E402
    _frame,
    _guard_timing,
    _latest_decision_log,
    _read_intents,
    _sessions,
)
from test_b15_sizing_reservations import (  # noqa: E402
    NVDA_FALLING,
    _broker_state,
    _env,
    _nvda_limit,
    _open_order_coid,
    _order_for,
    _run_main,
    _write_fixture,
)

REPO = Path(__file__).resolve().parent
VENV_PY = REPO / ".venv" / "bin" / "python"


# ---------------------------------------------------------------- helpers

def _reset_submitted_today(home: Path) -> None:
    """Clear submitted_today only (simulate the next day), keeping
    open_entries + expected_positions — unlike B13's
    _reset_decision_dedupe, which clears both."""
    path = home / "strategy_state.json"
    state = json.loads(path.read_text())
    state["submitted_today"] = []
    path.write_text(json.dumps(state))


def _strategy_state(home: Path) -> dict:
    return json.loads((home / "strategy_state.json").read_text())


def _order_count(fixture_dir: Path) -> int:
    return len(_broker_state(fixture_dir)["orders"])


def _intent_by_id(home: Path, intent_id: str) -> dict:
    matches = [i for i in _read_intents(home) if i["intent_id"] == intent_id]
    assert len(matches) == 1, intent_id
    return matches[0]


def _run_buy(home: Path, fixture: Path, *, exit_on_signal_loss: int = 0):
    """Run 1 of the e2e scenarios: a live run that buys NVDA qty 2
    (limit, unfilled). Returns (log, coid, limit)."""
    lasts = _write_fixture(fixture)
    nvda_last = lasts["NVDA"]
    limit = _nvda_limit(nvda_last)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"],
                        exit_on_signal_loss=exit_on_signal_loss)
    assert rc == 0, out
    log = _latest_decision_log(home)
    assert log["status"] == "ok", out
    nvda = _order_for(log, "NVDA")
    assert nvda["order"]["qty"] == 2 and nvda["order"]["limit_price"] == limit
    return log, _open_order_coid(fixture, "NVDA"), limit


# ---------------------------------------------------------------- unit tests:
# OrderManager — expired / done_for_day mapping + reconcile_open_intents

def test_adopt_expired_and_done_for_day_are_cancelled(tmp_path):
    from order_manager import OrderManager, build_intent

    manager = OrderManager(str(tmp_path / "intents.db"))
    intent = build_intent(design_id="live-trend", trading_day="2026-11-25",
                     symbol="NVDA", side="buy", qty=2.0,
                     order_type="limit", limit_price=167.25,
                     inputs_hash="abc", account="paper")
    manager.submit(intent, place_order_fn=lambda o: {
        "id": "fake-0001", "status": "submitted",
        "broker_order_id": "fake-0001", "submitted": o},
        lookup_fn=lambda coid: {"id": "fake-0001", "status": "submitted",
                                "filled_qty": 0.0})
    adopted = manager.reconcile_open_intents(
        lookup_fn=lambda coid: {"id": "fake-0001", "status": "expired",
                                "filled_qty": 0.0})
    assert len(adopted) == 1
    assert adopted[0]["status"] == "CANCELLED"
    assert adopted[0]["broker_order_id"] == "fake-0001"


def test_adopt_sdk_enum_repr_expired(tmp_path):
    """str(OrderStatus.EXPIRED) is 'OrderStatus.EXPIRED' — the SDK
    path must map it to CANCELLED, not SUBMITTED (the pre-B16 bug:
    'orderstatus.expired' fell through to the else branch)."""
    from order_manager import OrderManager, build_intent

    manager = OrderManager(str(tmp_path / "intents.db"))
    intent = build_intent(design_id="live-trend", trading_day="2026-11-25",
                     symbol="NVDA", side="buy", qty=2.0,
                     order_type="limit", limit_price=167.25,
                     inputs_hash="abc", account="paper")
    manager.submit(intent, place_order_fn=lambda o: {
        "id": "fake-0001", "status": "submitted",
        "broker_order_id": "fake-0001", "submitted": o},
        lookup_fn=lambda coid: {"id": "fake-0001", "status": "submitted",
                                "filled_qty": 0.0})
    adopted = manager.reconcile_open_intents(
        lookup_fn=lambda coid: {"id": "fake-0001",
                                "status": "OrderStatus.EXPIRED",
                                "filled_qty": 0.0})
    assert adopted[0]["status"] == "CANCELLED"


def test_adopt_done_for_day_and_rejected(tmp_path):
    from order_manager import OrderManager, build_intent

    manager = OrderManager(str(tmp_path / "intents.db"))
    for status, expected in (("done_for_day", "CANCELLED"),
                             ("rejected", "REJECTED"),
                             ("canceled", "CANCELLED"),
                             ("new", "SUBMITTED")):
        intent = build_intent(design_id="live-trend", trading_day="2026-11-25",
                         symbol="NVDA", side="buy", qty=2.0,
                         order_type="limit", limit_price=167.25,
                         inputs_hash=f"abc-{status}", account="paper")
        manager.submit(intent, place_order_fn=lambda o, i=intent: {
            "id": f"fake-{i['intent_id'][:4]}", "status": "submitted",
            "broker_order_id": f"fake-{i['intent_id'][:4]}", "submitted": o},
            lookup_fn=lambda coid: {"id": "x", "status": "submitted",
                                    "filled_qty": 0.0})
        adopted = manager.reconcile_open_intents(
            lookup_fn=lambda coid, s=status: {"id": "x", "status": s,
                                              "filled_qty": 0.0})
        assert adopted[0]["status"] == expected, status


def test_reconcile_open_intents_inconclusive_leaves_intent(tmp_path):
    from order_manager import OrderManager, build_intent

    manager = OrderManager(str(tmp_path / "intents.db"))
    intent = build_intent(design_id="live-trend", trading_day="2026-11-25",
                     symbol="NVDA", side="buy", qty=2.0,
                     order_type="limit", limit_price=167.25,
                     inputs_hash="abc", account="paper")
    manager.submit(intent, place_order_fn=lambda o: {
        "id": "fake-0001", "status": "submitted",
        "broker_order_id": "fake-0001", "submitted": o},
        lookup_fn=lambda coid: {"id": "fake-0001", "status": "submitted",
                                "filled_qty": 0.0})
    # Lookup returns None, then raises: the intent stays SUBMITTED,
    # no alert, no block — a confirmed-accepted order is not
    # ambiguous in the Loop 5 sense.
    assert manager.reconcile_open_intents(lookup_fn=lambda coid: None) == []
    assert manager.get_intent(intent["intent_id"])["status"] == "SUBMITTED"
    assert manager.reconcile_open_intents(
        lookup_fn=lambda coid: (_ for _ in ()).throw(RuntimeError("boom"))) == []
    assert manager.get_intent(intent["intent_id"])["status"] == "SUBMITTED"


def test_cancelled_intents_lists_only_cancelled(tmp_path):
    from order_manager import OrderManager, build_intent

    manager = OrderManager(str(tmp_path / "intents.db"))
    ids = {}
    for tag, status in (("one", "expired"), ("two", "submitted")):
        intent = build_intent(design_id="live-trend", trading_day="2026-11-25",
                         symbol="NVDA", side="buy", qty=2.0,
                         order_type="limit", limit_price=167.25,
                         inputs_hash=f"abc-{tag}", account="paper")
        ids[tag] = intent["intent_id"]
        broker_id = f"fake-{tag}"
        manager.submit(intent, place_order_fn=lambda o, bid=broker_id: {
            "id": bid, "status": "submitted",
            "broker_order_id": bid, "submitted": o},
            lookup_fn=lambda coid, bid=broker_id: {"id": bid, "status": "submitted",
                                                   "filled_qty": 0.0})
    # Only the first intent's broker order expired.
    manager.reconcile_open_intents(
        lookup_fn=lambda coid: {"id": "fake-one", "status": "expired",
                                "filled_qty": 0.0}
        if coid == ids["one"] else
        {"id": "fake-two", "status": "submitted", "filled_qty": 0.0})
    cancelled = manager.cancelled_intents()
    assert len(cancelled) == 1
    assert cancelled[0]["status"] == "CANCELLED"
    assert cancelled[0]["broker_order_id"] == "fake-one"


# ---------------------------------------------------------------- unit tests:
# the state mirror

def _state_with_entry(intent_id, qty=2.0, side="buy"):
    from strategy_runner import StrategyState

    state = StrategyState()
    state.open_entries["NVDA"] = {
        "symbol": "NVDA", "qty": qty, "side": side, "direction": "mild_up",
        "decision_id": "d1", "lot_id": None, "entry_price": 167.25,
        "opened_at": "2026-11-25T20:00:00+00:00", "intent_id": intent_id,
    }
    state.expected_positions["paper"]["NVDA"] = {"qty": qty, "decision_id": "d1"}
    return state


def test_mirror_unfilled_removes_entry():
    from strategy_runner import _mirror_cancelled_intents_into_state

    state = _state_with_entry("intent-1")
    adjustments = _mirror_cancelled_intents_into_state(state, [
        {"intent_id": "intent-1", "symbol": "NVDA", "account": "paper",
         "status": "CANCELLED", "filled_qty": 0.0}])
    assert adjustments == [{"symbol": "NVDA", "intent_id": "intent-1",
                            "filled_qty": 0.0, "action": "removed"}]
    assert state.open_entries == {}
    assert state.expected_positions["paper"] == {}


def test_mirror_partial_fill_keeps_filled_only():
    from strategy_runner import _mirror_cancelled_intents_into_state

    state = _state_with_entry("intent-1")
    adjustments = _mirror_cancelled_intents_into_state(state, [
        {"intent_id": "intent-1", "symbol": "NVDA", "account": "paper",
         "status": "CANCELLED", "filled_qty": 1.0}])
    assert adjustments == [{"symbol": "NVDA", "intent_id": "intent-1",
                            "filled_qty": 1.0, "action": "reduced_to_filled"}]
    assert state.open_entries["NVDA"]["qty"] == 1.0
    assert state.expected_positions["paper"]["NVDA"]["qty"] == 1.0


def test_mirror_skips_unmatched_and_no_intent_id():
    from strategy_runner import _mirror_cancelled_intents_into_state

    state = _state_with_entry("intent-1")
    # Different intent id: another order for the same symbol.
    adjustments = _mirror_cancelled_intents_into_state(state, [
        {"intent_id": "intent-2", "symbol": "NVDA", "account": "paper",
         "status": "CANCELLED", "filled_qty": 0.0}])
    assert adjustments == []
    assert state.open_entries["NVDA"]["qty"] == 2.0
    # No intent_id recorded (pre-B16 entry): skipped — the end-of-run
    # reconcile remains the backstop, no guessing.
    state.open_entries["NVDA"]["intent_id"] = None
    adjustments = _mirror_cancelled_intents_into_state(state, [
        {"intent_id": "intent-1", "symbol": "NVDA", "account": "paper",
         "status": "CANCELLED", "filled_qty": 0.0}])
    assert adjustments == []
    assert state.open_entries["NVDA"]["qty"] == 2.0
    # A non-CANCELLED intent is not mirrored at all.
    state.open_entries["NVDA"]["intent_id"] = "intent-1"
    adjustments = _mirror_cancelled_intents_into_state(state, [
        {"intent_id": "intent-1", "symbol": "NVDA", "account": "paper",
         "status": "SUBMITTED", "filled_qty": 0.0}])
    assert adjustments == []


# ---------------------------------------------------------------- unit tests:
# the cancel-first helper

class _StubLeg:
    """A leg stub for _cancel_working_orders_on_leg: open_orders()
    mirrors the broker rule (terminal statuses excluded), cancel_order
    marks the order cancelled unless a custom impl is given."""

    def __init__(self, orders, positions=None, cancel_impl=None):
        self._orders = [dict(o) for o in orders]
        self._positions = positions or {}
        self.cancel_calls = []
        self._cancel_impl = cancel_impl

    def open_orders(self):
        return [dict(o) for o in self._orders
                if str(o.get("status", "")).lower() not in {
                    "cancelled", "canceled", "rejected", "filled",
                    "expired", "done_for_day"}]

    def positions(self):
        return dict(self._positions)

    def cancel_order(self, order_id=None, client_order_id=None):
        self.cancel_calls.append((order_id, client_order_id))
        if self._cancel_impl is not None:
            return self._cancel_impl(order_id, client_order_id)
        for order in self._orders:
            if str(order.get("id")) == str(order_id):
                order["status"] = "cancelled"
                return dict(order)
        return None


def _working_buy(order_id="fake-0001", qty=2.0, filled=1.0):
    return {"id": order_id, "symbol": "NVDA", "side": "buy", "qty": qty,
            "filled_qty": filled, "status": "submitted"}


def test_cancel_helper_cancels_working_buy_and_reports_filled():
    from strategy_runner import _cancel_working_orders_on_leg

    leg = _StubLeg([_working_buy()])
    ok, filled, reason = _cancel_working_orders_on_leg(leg, "NVDA", "buy")
    assert ok and filled == 1.0 and reason == ""
    assert leg.cancel_calls == [("fake-0001", None)]
    assert leg.open_orders() == []


def test_cancel_helper_no_working_order_uses_positions():
    from strategy_runner import _cancel_working_orders_on_leg

    leg = _StubLeg([], positions={"NVDA": {"qty": 2.0}})
    ok, filled, reason = _cancel_working_orders_on_leg(leg, "NVDA", "buy")
    assert ok and filled == 2.0 and reason == ""
    assert leg.cancel_calls == []


def test_cancel_helper_cancel_failure_is_not_ok():
    from strategy_runner import _cancel_working_orders_on_leg

    def _boom(order_id, client_order_id):
        raise RuntimeError("broker 500")

    leg = _StubLeg([_working_buy()], cancel_impl=_boom)
    ok, filled, reason = _cancel_working_orders_on_leg(leg, "NVDA", "buy")
    assert not ok and filled == 0.0
    assert "cancel failed" in reason


def test_cancel_helper_still_open_after_cancel_is_not_ok():
    from strategy_runner import _cancel_working_orders_on_leg

    def _noop(order_id, client_order_id):
        return {"id": order_id, "status": "submitted", "filled_qty": 1.0}

    leg = _StubLeg([_working_buy()], cancel_impl=_noop)
    ok, filled, reason = _cancel_working_orders_on_leg(leg, "NVDA", "buy")
    assert not ok and filled == 0.0
    assert "did not take effect" in reason


def test_cancel_helper_leg_without_cancel_is_not_ok():
    from strategy_runner import _cancel_working_orders_on_leg

    class _NoCancelLeg(_StubLeg):
        cancel_order = None

    leg = _NoCancelLeg([_working_buy()])
    ok, filled, reason = _cancel_working_orders_on_leg(leg, "NVDA", "buy")
    assert not ok and filled == 0.0
    assert "no cancel_order" in reason


def test_cancel_helper_intent_tripwire_vetoes_working_status(tmp_path):
    from order_manager import OrderManager, build_intent
    from strategy_runner import _cancel_working_orders_on_leg

    manager = OrderManager(str(tmp_path / "intents.db"))
    intent = build_intent(design_id="live-trend", trading_day="2026-11-25",
                     symbol="NVDA", side="buy", qty=2.0,
                     order_type="limit", limit_price=167.25,
                     inputs_hash="abc", account="paper")
    manager.submit(intent, place_order_fn=lambda o: {
        "id": "fake-0001", "status": "submitted",
        "broker_order_id": "fake-0001", "submitted": o},
        lookup_fn=lambda coid: {"id": "fake-0001", "status": "submitted",
                                "filled_qty": 0.0})
    leg = _StubLeg([], positions={"NVDA": {"qty": 1.0}})
    # The broker lookup says the entry is still working -> veto.
    ok, _, reason = _cancel_working_orders_on_leg(
        leg, "NVDA", "buy", manager=manager, intent_id=intent["intent_id"],
        lookup_fn=lambda coid: {"id": "fake-0001", "status": "new",
                                "filled_qty": 1.0})
    assert not ok and "still new" in reason
    # The broker lookup says the entry is cancelled -> the exit may go.
    ok, filled, reason = _cancel_working_orders_on_leg(
        leg, "NVDA", "buy", manager=manager, intent_id=intent["intent_id"],
        lookup_fn=lambda coid: {"id": "fake-0001", "status": "cancelled",
                                "filled_qty": 1.0})
    assert ok and filled == 1.0 and reason == ""
    # A lookup that returns None relies on the re-fetch (already
    # confirmed: no working orders) — it can only veto, never falsely
    # confirm.
    ok, filled, reason = _cancel_working_orders_on_leg(
        leg, "NVDA", "buy", manager=manager, intent_id=intent["intent_id"],
        lookup_fn=lambda coid: None)
    assert ok and filled == 1.0


# ---------------------------------------------------------------- unit tests:
# AlpacaLeg.cancel_order / FakeLeg.cancel_order / FakeBroker.expire

def _leg(monkeypatch):
    from broker_alpaca import AccountConfig, AlpacaLeg

    monkeypatch.setenv("ALPACA_PAPER_KEY", "dummy")
    monkeypatch.setenv("ALPACA_PAPER_SECRET", "dummy")
    return AlpacaLeg(AccountConfig("paper", 100_000.0, 0.10, True), 0.25, {})


def test_alpaca_cancel_order_sdk(monkeypatch):
    leg = _leg(monkeypatch)
    cancelled = []

    class _Client:
        def cancel_order_by_id(self, order_id):
            cancelled.append(order_id)
            return None  # the SDK cancel returns None, per its docstring

        def get_order_by_id(self, order_id):
            return SimpleNamespace(id=order_id, symbol="NVDA",
                                   status="canceled", filled_qty="1")

    leg.client = _Client()
    result = leg.cancel_order(order_id="ord-1")
    assert cancelled == ["ord-1"]
    assert result == {"id": "ord-1", "symbol": "NVDA", "status": "canceled",
                      "filled_qty": 1.0}


def test_alpaca_cancel_order_sdk_404_is_success(monkeypatch):
    leg = _leg(monkeypatch)

    class _Client:
        def cancel_order_by_id(self, order_id):
            raise RuntimeError("404 order not found")

    leg.client = _Client()
    assert leg.cancel_order(order_id="ord-1") is None


def test_alpaca_cancel_order_rest(monkeypatch):
    leg = _leg(monkeypatch)
    leg.client = None
    calls = []

    def _rest(method, path, payload=None):
        calls.append((method, path))
        if method == "DELETE":
            return {}  # REST DELETE returns 204 (empty body)
        return {"id": "ord-1", "symbol": "NVDA", "status": "cancelled",
                "filled_qty": "2"}

    leg._rest_json = _rest
    result = leg.cancel_order(order_id="ord-1")
    assert calls[0] == ("DELETE", f"{leg._base_url}/v2/orders/ord-1")
    assert result["status"] == "cancelled" and result["filled_qty"] == 2.0


def test_alpaca_cancel_order_rest_404_is_success(monkeypatch):
    leg = _leg(monkeypatch)
    leg.client = None
    leg._rest_json = lambda method, path, payload=None: (_ for _ in ()).throw(
        RuntimeError("alpaca DELETE https://x/v2/orders/ord-1 failed: 404 {}"))
    assert leg.cancel_order(order_id="ord-1") is None


def test_alpaca_cancel_order_by_client_order_id_rest(monkeypatch):
    leg = _leg(monkeypatch)
    leg.client = None
    calls = []

    def _rest(method, path, payload=None):
        calls.append((method, path))
        if method == "DELETE":
            return {}
        return {"id": "ord-1", "symbol": "NVDA", "status": "cancelled",
                "filled_qty": "0"}

    leg._rest_json = _rest
    result = leg.cancel_order(client_order_id="abc123")
    assert calls[0][1].endswith("/v2/orders:client_order_id/abc123")
    assert result["status"] == "cancelled"


def test_alpaca_cancel_order_disabled_leg_returns_none(monkeypatch):
    from broker_alpaca import AccountConfig, AlpacaLeg

    monkeypatch.setenv("ALPACA_PAPER_KEY", "dummy")
    monkeypatch.setenv("ALPACA_PAPER_SECRET", "dummy")
    leg = AlpacaLeg(AccountConfig("paper", 100_000.0, 0.10, False), 0.25, {})
    assert leg.cancel_order(order_id="ord-1") is None


def test_fake_leg_cancel_order(tmp_path):
    from fake_dual_broker import FakeDualBroker

    fixture = tmp_path / "fixture"
    _write_fixture(fixture)
    broker = FakeDualBroker(fixture)
    order = broker.paper.submit({
        "symbol": "NVDA", "side": "buy", "qty": 2.0, "order_type": "limit",
        "limit_price": 167.25, "client_order_id": "coid-1"})
    by_id = broker.paper.cancel_order(order_id=order["id"])
    assert by_id["status"] == "cancelled" and by_id["filled_qty"] == 0.0
    assert broker.paper.open_orders() == []
    # Cancelling again by client_order_id is a no-op success.
    again = broker.paper.cancel_order(client_order_id="coid-1")
    assert again["status"] == "cancelled"
    # The disabled live leg has nothing to cancel.
    assert broker.live.cancel_order(order_id="ord-1") is None
    assert broker.live.cancel_order(client_order_id="coid-1") is None


def test_fake_broker_expire_and_fill_after_cancel_is_noop(tmp_path):
    from fake_broker import FakeBroker
    from fake_dual_broker import FakeDualBroker, FileBackedFakeBroker

    fixture = tmp_path / "fixture"
    _write_fixture(fixture)
    broker = FakeDualBroker(fixture)
    coid = "coid-expire"
    broker.paper.submit({
        "symbol": "NVDA", "side": "buy", "qty": 2.0, "order_type": "limit",
        "limit_price": 167.25, "client_order_id": coid})
    store = FileBackedFakeBroker(fixture)
    expired = store.expire(coid)
    assert expired["status"] == "expired"
    assert expired["filled_qty"] == 0.0
    # A fresh broker (as a new process would) sees the expired order
    # as not open.
    assert FakeDualBroker(fixture).paper.open_orders() == []
    # A fill delivered after expiry must not re-open the position.
    fill = store.fill(coid, "fill-late", 1, 167.25)
    assert fill["qty"] == 0.0
    state = _broker_state(fixture)
    assert state["orders"][coid]["status"] == "expired"
    assert state["orders"][coid]["filled_qty"] == 0.0

    # Same for a cancelled order (in-memory fake, no file round-trip).
    broker = FakeBroker()
    broker.submit({"symbol": "NVDA", "side": "buy", "qty": 2.0,
                   "order_type": "limit", "limit_price": 167.25,
                   "client_order_id": "coid-cancel"})
    broker.cancel("coid-cancel")
    fill = broker.apply_partial_fill("coid-cancel", "fill-1", 1, 167.25)
    assert fill["qty"] == 0.0
    assert broker.get_by_client_order_id("coid-cancel")["filled_qty"] == 0.0


# ---------------------------------------------------------------- e2e

def test_e2e_unfilled_day_order_expires_overnight(tmp_path):
    """(1) Run 1 submits the buy (limit, unfilled); the broker marks
    it expired overnight. Run 2 (next day) does NOT drift-halt, the
    stale expected position is cleared, and NVDA can be re-selected
    (a NEW buy, new client_order_id)."""
    _guard_timing()
    home = tmp_path / "home"
    fixture = tmp_path / "fixture"
    home.mkdir(parents=True)
    _, coid, _ = _run_buy(home, fixture)

    from fake_dual_broker import FileBackedFakeBroker
    FileBackedFakeBroker(fixture).expire(coid)
    assert _broker_state(fixture)["orders"][coid]["status"] == "expired"

    # Next day: fresh process, new prices (a different cycle, so the
    # re-selection derives a NEW intent — otherwise submit() would
    # return the terminal CANCELLED intent untouched and place no
    # new order).
    _reset_submitted_today(home)
    _write_fixture(fixture, nvda_cycle=(0.008, 0.008, -0.008))
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"])
    assert rc == 0, out
    log2 = _latest_decision_log(home)
    assert log2["status"] == "ok", out  # NOT needs_attention: no drift halt
    assert log2["state_sync"] == [{
        "symbol": "NVDA", "intent_id": coid,
        "filled_qty": 0.0, "action": "removed"}]
    assert _intent_by_id(home, coid)["status"] == "CANCELLED"
    new_buy = _order_for(log2, "NVDA")
    # A new buy was submitted (new intent, new broker order), not a
    # re-adoption of the cancelled one.
    new_intents = [i for i in _read_intents(home)
                   if i["intent_id"] != coid]
    assert len(new_intents) == 1
    assert new_intents[0]["status"] == "SUBMITTED"
    assert new_intents[0]["broker_order_id"] == "fake-0002"
    assert _order_count(fixture) == 2
    state = _strategy_state(home)
    assert state["open_entries"]["NVDA"]["qty"] == new_buy["order"]["qty"]
    assert state["expected_positions"]["paper"]["NVDA"]["qty"] == new_buy["order"]["qty"]


def test_e2e_partial_fill_then_cancel_keeps_filled_only(tmp_path):
    """(2) Run 1 buys NVDA qty 2, fills 1, remainder cancelled.
    Run 2: expected position becomes the filled qty only; reconcile
    passes; sizing counts the filled position only (no reservation
    for the cancelled remainder)."""
    _guard_timing()
    home = tmp_path / "home"
    fixture = tmp_path / "fixture"
    home.mkdir(parents=True)
    _, coid, limit = _run_buy(home, fixture)

    from fake_dual_broker import FileBackedFakeBroker
    store = FileBackedFakeBroker(fixture)
    store.fill(coid, "fill-1", 1, limit)
    store.cancel(coid)
    assert _broker_state(fixture)["orders"][coid]["status"] == "cancelled"

    _reset_submitted_today(home)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"])
    assert rc == 0, out
    log2 = _latest_decision_log(home)
    assert log2["status"] == "ok", out
    assert log2["state_sync"] == [{
        "symbol": "NVDA", "intent_id": coid,
        "filled_qty": 1.0, "action": "reduced_to_filled"}]
    assert _intent_by_id(home, coid)["status"] == "CANCELLED"
    state = _strategy_state(home)
    assert state["open_entries"]["NVDA"]["qty"] == 1.0
    assert state["expected_positions"]["paper"]["NVDA"]["qty"] == 1.0
    # No new order (NVDA is held), and the budget counts the filled
    # share only: 1 x last close, no reservation for the cancelled
    # remainder.
    assert log2["orders"] == []
    lasts = _write_fixture(fixture)  # rewrite only to read the last price
    budget = log2["budget"]
    assert budget["existing_notional"] == round(1 * lasts["NVDA"], 2), budget
    assert budget["open_buy_reservations"] == 0.0


def test_e2e_exit_cancels_working_buy_first(tmp_path):
    """(3) Entry partially filled, buy still working, signal lost ->
    the buy is cancelled FIRST, the exit sells only the filled qty,
    and a later fill attempt on the cancelled order never re-opens
    the position."""
    _guard_timing()
    home = tmp_path / "home"
    fixture = tmp_path / "fixture"
    home.mkdir(parents=True)
    _, coid, limit = _run_buy(home, fixture, exit_on_signal_loss=1)

    from fake_dual_broker import FileBackedFakeBroker
    store = FileBackedFakeBroker(fixture)
    store.fill(coid, "fill-1", 1, limit)

    _reset_submitted_today(home)
    _write_fixture(fixture, nvda_cycle=NVDA_FALLING)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"],
                        exit_on_signal_loss=1)
    assert rc == 0, out
    log2 = _latest_decision_log(home)
    assert log2["status"] == "ok", out
    assert log2["orders"] == []  # no new entry; the exit is not a preview order
    orders = _broker_state(fixture)["orders"]
    assert len(orders) == 2
    buy, sell = sorted(orders.values(), key=lambda o: o["id"])
    assert buy["side"] == "buy" and buy["status"] == "cancelled"
    assert buy["filled_qty"] == 1.0
    assert sell["side"] == "sell" and sell["qty"] == 1.0
    assert sell["status"] in {"submitted", "new"}
    intents = _read_intents(home)
    assert len(intents) == 2
    exit_intent = intents[1]
    assert exit_intent["side"] == "sell" and exit_intent["qty"] == 1.0
    assert exit_intent["status"] == "SUBMITTED"
    assert exit_intent["broker_order_id"] == "fake-0002"
    assert _strategy_state(home)["open_entries"] == {}

    # A late fill attempt on the cancelled buy is a no-op: the
    # position never re-opens. (A fresh store, as a new process would
    # load — reusing the pre-Run-2 instance would see the stale
    # pre-cancel order and overwrite Run 2's broker state.)
    FileBackedFakeBroker(fixture).fill(coid, "fill-late", 1, limit)
    state = _broker_state(fixture)
    assert state["orders"][coid]["status"] == "cancelled"
    assert state["orders"][coid]["filled_qty"] == 1.0

    _reset_submitted_today(home)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"],
                        exit_on_signal_loss=1)
    assert rc == 0, out
    log3 = _latest_decision_log(home)
    assert log3["status"] == "ok", out
    assert log3["orders"] == []
    assert _strategy_state(home)["open_entries"] == {}
    assert _order_count(fixture) == 2


def test_e2e_exit_with_unfilled_buy_closes_without_selling(tmp_path):
    """(3b) The buy never filled and the signal is lost: the exit
    cancels the working buy and closes the entry WITHOUT submitting
    a sell (nothing held)."""
    _guard_timing()
    home = tmp_path / "home"
    fixture = tmp_path / "fixture"
    home.mkdir(parents=True)
    _, coid, _ = _run_buy(home, fixture, exit_on_signal_loss=1)

    _reset_submitted_today(home)
    _write_fixture(fixture, nvda_cycle=NVDA_FALLING)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"],
                        exit_on_signal_loss=1)
    assert rc == 0, out
    log2 = _latest_decision_log(home)
    assert log2["status"] == "ok", out
    orders = _broker_state(fixture)["orders"]
    assert len(orders) == 1  # only the (cancelled) buy; no sell
    assert orders[coid]["status"] == "cancelled"
    assert orders[coid]["filled_qty"] == 0.0
    assert len(_read_intents(home)) == 1  # no exit intent was created
    assert _strategy_state(home)["open_entries"] == {}


def test_e2e_no_open_orders_unchanged(tmp_path):
    """(4) Regression: no open orders -> behaviour identical to
    before (startup sync empty, one order, intent SUBMITTED)."""
    _guard_timing()
    home = tmp_path / "home"
    fixture = tmp_path / "fixture"
    home.mkdir(parents=True)
    lasts = _write_fixture(fixture)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"])
    assert rc == 0, out
    log = _latest_decision_log(home)
    assert log["status"] == "ok", out
    assert log["state_sync"] == []
    nvda = _order_for(log, "NVDA")
    assert nvda["order"]["qty"] == 2
    assert nvda["order"]["limit_price"] == _nvda_limit(lasts["NVDA"])
    intents = _read_intents(home)
    assert len(intents) == 1
    assert intents[0]["status"] == "SUBMITTED"
    assert intents[0]["broker_order_id"] == "fake-0001"
