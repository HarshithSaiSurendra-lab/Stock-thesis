"""B3 exact-once tests. Fake broker only — no real Alpaca, no network.

Acceptance: one logical order produces at most one broker order.
"""
import threading

import pytest

from fake_broker import FakeBroker
from order_manager import OrderManager, build_intent, derive_client_order_id

NO_SLEEP = lambda s: None  # noqa: E731


def _intent(design="v2", day="2026-12-01", symbol="AAPL", qty=10, inputs_hash="abc123"):
    return build_intent(design, day, symbol, "buy", qty, "market", None, inputs_hash)


def test_1_accept_drops_response_restart_delayed_lookup_recovers(tmp_path):
    broker = FakeBroker(delayed_visibility_polls=2, drop_next_response=True)
    db = tmp_path / "intents.db"
    manager = OrderManager(db)
    intent = _intent()
    result = manager.submit(
        intent, place_order_fn=broker.submit,
        lookup_fn=broker.get_by_client_order_id,
        sleep_fn=NO_SLEEP, grace_seconds=0,
    )
    assert result["status"] == "SUBMITTED"
    assert result["broker_order_id"]
    assert broker.order_count() == 1
    manager.close()

    # "Restart": a fresh manager on the same DB file.
    manager2 = OrderManager(db)
    try:
        recovered = manager2.reconcile_all_unknowns(
            lookup_fn=broker.get_by_client_order_id,
            place_order_fn=broker.submit, grace_seconds=0, sleep_fn=NO_SLEEP,
        )
        # Already SUBMITTED, so nothing pending to recover — and no
        # second order was created by the restart.
        assert broker.order_count() == 1
        again = manager2.submit(
            intent, place_order_fn=broker.submit,
            lookup_fn=broker.get_by_client_order_id, sleep_fn=NO_SLEEP, grace_seconds=0,
        )
        assert again["broker_order_id"] == result["broker_order_id"]
        assert broker.order_count() == 1
    finally:
        manager2.close()


def test_1b_not_found_after_grace_resubmit_same_id(tmp_path):
    # Lookup never finds the order (delayed visibility far beyond the
    # poll budget), so reconcile must resubmit with the SAME ID.
    broker = FakeBroker(delayed_visibility_polls=999, drop_next_response=True)
    manager = OrderManager(tmp_path / "intents.db")
    try:
        intent = _intent()
        result = manager.submit(
            intent, place_order_fn=broker.submit,
            lookup_fn=broker.get_by_client_order_id,
            sleep_fn=NO_SLEEP, grace_seconds=0, max_polls=3,
        )
        assert result["status"] == "SUBMITTED"
        assert result["intent_id"] == intent["intent_id"]
        assert broker.order_count() == 1
        assert broker.list_orders()[0]["client_order_id"] == intent["intent_id"]
    finally:
        manager.close()


def test_2_two_workers_same_intent_concurrently(tmp_path):
    broker = FakeBroker()
    db = tmp_path / "intents.db"
    intent = _intent()
    barrier = threading.Barrier(2)
    results = {}

    def worker(name):
        import time as _time

        mgr = OrderManager(db)
        try:
            barrier.wait(timeout=10)
            results[name] = mgr.submit(
                intent, place_order_fn=broker.submit,
                lookup_fn=broker.get_by_client_order_id,
                claimed_by=name, sleep_fn=lambda s: _time.sleep(0.005), grace_seconds=0,
            )
        finally:
            mgr.close()

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert not any(t.is_alive() for t in threads)
    assert broker.order_count() == 1
    assert results["w0"]["broker_order_id"] == results["w1"]["broker_order_id"]
    assert results["w0"]["intent_id"] == results["w1"]["intent_id"] == intent["intent_id"]


def test_3_partial_fill_restart_duplicate_fill_exactly_once(tmp_path):
    broker = FakeBroker()
    db = tmp_path / "intents.db"
    manager = OrderManager(db)
    intent = _intent(qty=10)
    result = manager.submit(
        intent, place_order_fn=broker.submit,
        lookup_fn=broker.get_by_client_order_id, sleep_fn=NO_SLEEP, grace_seconds=0,
    )
    fill = broker.apply_partial_fill(result["intent_id"], "fill-1", 4, 100.0)
    applied, filled, remaining = manager.apply_fill_update(result["intent_id"], fill)
    assert (applied, filled, remaining) == (True, 4.0, 6.0)
    manager.close()

    # Restart, then the broker repeats the same fill_id delivery.
    manager2 = OrderManager(db)
    try:
        replay = broker.duplicate_fill_delivery(fill)
        applied, filled, remaining = manager2.apply_fill_update(result["intent_id"], replay)
        assert applied is False
        assert (filled, remaining) == (4.0, 6.0)
        state = manager2.get_fill_state(result["intent_id"])
        assert state["filled_qty"] == 4.0 and state["remaining_qty"] == 6.0
        assert len(state["fills"]) == 1

        # A second, distinct fill for the remaining 6 completes the order.
        fill2 = broker.apply_partial_fill(result["intent_id"], "fill-2", 6, 101.0)
        applied, filled, remaining = manager2.apply_fill_update(result["intent_id"], fill2)
        assert (applied, filled, remaining) == (True, 10.0, 0.0)
        assert manager2.get_intent(result["intent_id"])["status"] == "FILLED"
    finally:
        manager2.close()


def test_3b_cumulative_snapshot_replay_is_noop(tmp_path):
    broker = FakeBroker()
    manager = OrderManager(tmp_path / "intents.db")
    try:
        intent = _intent(qty=10)
        result = manager.submit(intent, place_order_fn=broker.submit,
                                lookup_fn=broker.get_by_client_order_id,
                                sleep_fn=NO_SLEEP, grace_seconds=0)
        fill = broker.apply_partial_fill(result["intent_id"], "fill-1", 4, 100.0)
        manager.apply_fill_update(result["intent_id"], fill)
        # Same cumulative total, new fill_id: must not add again.
        applied, filled, remaining = manager.apply_fill_update(
            result["intent_id"],
            {"fill_id": "fill-1-snapshot", "qty": 0, "cumulative_filled_qty": 4.0, "price": 100.0},
        )
        assert applied is False and (filled, remaining) == (4.0, 6.0)
    finally:
        manager.close()


def test_4_startup_reconcile_recovers_unknown_before_new_orders(tmp_path):
    broker = FakeBroker(delayed_visibility_polls=0)
    db = tmp_path / "intents.db"
    manager = OrderManager(db)

    # Simulate a submit whose response was lost AND whose resubmit
    # also timed out: broker accepted it, our side stays UNKNOWN.
    def place_then_timeout(order):
        try:
            broker.submit(order)
        except TimeoutError:
            raise
        raise TimeoutError("network down after acceptance")

    intent = _intent()
    # First call: broker stores it (drop flag off), our wrapper raises.
    result = manager.submit(
        intent, place_order_fn=place_then_timeout,
        lookup_fn=lambda coid: None,  # lookup also unavailable for now
        sleep_fn=NO_SLEEP, grace_seconds=0, max_polls=1,
    )
    # Reconcile's resubmit also raised TimeoutError, so it stays UNKNOWN.
    assert result["status"] == "UNKNOWN"
    assert broker.order_count() == 1
    manager.close()

    # Restart: startup reconcile (lookup now works) must recover it
    # BEFORE any new order for that symbol is accepted.
    manager2 = OrderManager(db)
    try:
        recovered = manager2.reconcile_all_unknowns(
            lookup_fn=broker.get_by_client_order_id,
            place_order_fn=broker.submit, grace_seconds=0, sleep_fn=NO_SLEEP,
        )
        assert len(recovered) == 1
        assert recovered[0]["status"] == "SUBMITTED"
        assert recovered[0]["broker_order_id"] == broker.list_orders()[0]["id"]
        assert broker.order_count() == 1
        # A new order for the same symbol now proceeds normally.
        intent2 = _intent(qty=5)
        result2 = manager2.submit(
            intent2, place_order_fn=broker.submit,
            lookup_fn=broker.get_by_client_order_id, sleep_fn=NO_SLEEP, grace_seconds=0,
        )
        assert result2["status"] == "SUBMITTED"
        assert broker.order_count() == 2
    finally:
        manager2.close()


def test_5_deterministic_replay_same_id_same_order(tmp_path):
    broker = FakeBroker()
    manager = OrderManager(tmp_path / "intents.db")
    try:
        first = manager.submit(_intent(), place_order_fn=broker.submit,
                               lookup_fn=broker.get_by_client_order_id,
                               sleep_fn=NO_SLEEP, grace_seconds=0)
        # Re-derive the identical intent from scratch (fresh dict).
        again = manager.submit(_intent(), place_order_fn=broker.submit,
                               lookup_fn=broker.get_by_client_order_id,
                               sleep_fn=NO_SLEEP, grace_seconds=0)
        assert again["intent_id"] == first["intent_id"]
        assert again["broker_order_id"] == first["broker_order_id"]
        assert broker.order_count() == 1
    finally:
        manager.close()


def test_deterministic_id_changes_with_design(tmp_path):
    a = derive_client_order_id("v2", "2026-12-01", "AAPL", "buy", 10, "market", None, "abc")
    b = derive_client_order_id("v3", "2026-12-01", "AAPL", "buy", 10, "market", None, "abc")
    c = derive_client_order_id("v2", "2026-12-01", "AAPL", "buy", 10, "market", None, "abc")
    assert a != b  # v2 vs v3, same symbol/qty/day: different intents
    assert a == c
    assert len(a) == 32 and all(ch in "0123456789abcdef" for ch in a)


def test_explicit_409_duplicate_is_terminal_no_second_submit(tmp_path):
    broker = FakeBroker()
    manager = OrderManager(tmp_path / "intents.db")
    calls = {"n": 0}

    def place_raising_409(order):
        calls["n"] += 1
        raise RuntimeError("409 duplicate client_order_id")

    try:
        result = manager.submit(
            _intent(), place_order_fn=place_raising_409,
            lookup_fn=lambda coid: None, sleep_fn=NO_SLEEP, grace_seconds=0,
        )
        assert result["status"] == "REJECTED"
        assert calls["n"] == 1  # no resubmit after an explicit rejection
        assert broker.order_count() == 0
    finally:
        manager.close()


def test_explicit_400_is_terminal(tmp_path):
    manager = OrderManager(tmp_path / "intents.db")
    calls = {"n": 0}

    def place_raising_400(order):
        calls["n"] += 1
        raise RuntimeError("400 validation error: qty must be positive")

    try:
        result = manager.submit(
            _intent(), place_order_fn=place_raising_400,
            lookup_fn=lambda coid: None, sleep_fn=NO_SLEEP, grace_seconds=0,
        )
        assert result["status"] == "REJECTED"
        assert calls["n"] == 1
    finally:
        manager.close()


def test_expired_claim_can_be_taken_over(tmp_path):
    broker = FakeBroker()
    manager = OrderManager(tmp_path / "intents.db", claim_lease_seconds=0.01)
    try:
        intent = _intent()
        manager._insert_intent(intent, "worker-a")
        assert manager._try_claim(intent["intent_id"], "worker-a") is True
        # Claim expires after the lease; another worker may take over.
        import time as _time

        _time.sleep(0.02)
        assert manager._try_claim(intent["intent_id"], "worker-b") is True
        result = manager.submit(
            intent, place_order_fn=broker.submit,
            lookup_fn=broker.get_by_client_order_id,
            claimed_by="worker-b", sleep_fn=NO_SLEEP, grace_seconds=0,
        )
        assert result["status"] == "SUBMITTED"
        assert broker.order_count() == 1
    finally:
        manager.close()
