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
            grace_seconds=0, sleep_fn=NO_SLEEP,
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


def test_1b_one_attempt_only_across_timeout_404s_lease_expiry_restart(tmp_path):
    """Loop 5 correction (Nov 26, 2026): exactly ONE submission attempt
    per logical order, ever. Timeout + repeated 404s + lease expiry +
    restart must never authorise a second attempt. reconcile() is
    lookup-only; an inconclusive lookup leaves the intent UNKNOWN,
    records an alert, and blocks the symbol.

    Two endings are covered:
    (a) a later lookup finds the original order -> adopted, 1 order;
    (b) the lookup never finds it -> stays UNKNOWN, alerted, blocked.
    """
    db = tmp_path / "intents.db"
    calls = {"place": 0}

    def counting_place(order):
        calls["place"] += 1
        return broker.submit(order)

    # --- ending (b): lookup never finds it --------------------------
    broker = FakeBroker(delayed_visibility_polls=999, drop_next_response=True)
    now = [1_000_000.0]
    manager = OrderManager(db, claim_lease_seconds=120.0, time_fn=lambda: now[0])
    try:
        intent = _intent()
        result = manager.submit(
            intent, place_order_fn=counting_place,
            lookup_fn=broker.get_by_client_order_id,
            sleep_fn=NO_SLEEP, grace_seconds=0, max_polls=3,
        )
        # The fake broker accepted it but dropped the response; every
        # lookup 404s. Exactly one attempt, intent UNKNOWN, alerted.
        assert calls["place"] == 1
        assert result["status"] == "UNKNOWN"
        assert result["attempts"] == 1
        assert broker.order_count() == 1
        assert manager.get_alerts(intent["intent_id"])
        assert manager.is_blocked("AAPL")

        # Repeated reconcile passes (more 404s) make no new attempt.
        for _ in range(3):
            again = manager.reconcile(
                result, lookup_fn=broker.get_by_client_order_id,
                grace_seconds=0, max_polls=3, sleep_fn=NO_SLEEP,
            )
            assert again["status"] == "UNKNOWN"
        assert calls["place"] == 1

        # A conflicting order for the same symbol is refused: the new
        # intent is BLOCKED and place is never called for it.
        blocked = manager.submit(
            _intent(qty=5), place_order_fn=counting_place,
            lookup_fn=broker.get_by_client_order_id, sleep_fn=NO_SLEEP, grace_seconds=0,
        )
        assert blocked["status"] == "BLOCKED"
        assert calls["place"] == 1
        assert broker.order_count() == 1
    finally:
        manager.close()

    # Lease expiry + restart: a fresh manager, clock past the lease.
    now[0] += 10_000.0
    manager2 = OrderManager(db, claim_lease_seconds=120.0, time_fn=lambda: now[0])
    try:
        again = manager2.submit(
            intent, place_order_fn=counting_place,
            lookup_fn=broker.get_by_client_order_id,
            sleep_fn=NO_SLEEP, grace_seconds=0, max_polls=3,
        )
        assert again["status"] == "UNKNOWN"
        assert calls["place"] == 1  # lease expiry did not authorise a 2nd attempt
        assert broker.order_count() == 1
        recovered = manager2.reconcile_all_unknowns(
            lookup_fn=broker.get_by_client_order_id,
            grace_seconds=0, sleep_fn=NO_SLEEP,
        )
        assert all(r["status"] == "UNKNOWN" for r in recovered)
        assert calls["place"] == 1
        assert manager2.is_blocked("AAPL")
    finally:
        manager2.close()

    # --- ending (a): a later lookup finds the original order --------
    # Same broker, same DB: the order was there all along; once the
    # delayed-visibility window passes, reconcile adopts it — still
    # with exactly one submission attempt and one broker order.
    broker._visibility_counts[intent["intent_id"]] = 999
    manager3 = OrderManager(db, claim_lease_seconds=120.0, time_fn=lambda: now[0])
    try:
        found = manager3.reconcile(
            manager3.get_intent(intent["intent_id"]),
            lookup_fn=broker.get_by_client_order_id,
            grace_seconds=0, max_polls=3, sleep_fn=NO_SLEEP,
        )
        assert found["status"] == "SUBMITTED"
        assert found["broker_order_id"] == broker.list_orders()[0]["id"]
        assert calls["place"] == 1
        assert broker.order_count() == 1
        assert not manager3.is_blocked("AAPL")
        assert manager3.get_alerts(intent["intent_id"]) == []  # resolved on adopt
        # The previously BLOCKED intent can now proceed normally.
        unblocked = manager3.submit(
            _intent(qty=5), place_order_fn=counting_place,
            lookup_fn=broker.get_by_client_order_id, sleep_fn=NO_SLEEP, grace_seconds=0,
        )
        assert unblocked["status"] == "SUBMITTED"
        assert calls["place"] == 2  # the new intent's first (only) attempt
        assert broker.order_count() == 2
    finally:
        manager3.close()


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
    # Lookup-only reconcile makes no second attempt, so it stays UNKNOWN.
    assert result["status"] == "UNKNOWN"
    assert broker.order_count() == 1
    manager.close()

    # Restart: startup reconcile (lookup now works) must recover it
    # BEFORE any new order for that symbol is accepted.
    manager2 = OrderManager(db)
    try:
        recovered = manager2.reconcile_all_unknowns(
            lookup_fn=broker.get_by_client_order_id,
            grace_seconds=0, sleep_fn=NO_SLEEP,
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


def test_explicit_409_duplicate_reconciles_before_reject(tmp_path):
    """A 409 conflict means the ID may already exist: reconcile the
    existing order FIRST. Only an inconclusive lookup leaves the
    intent UNKNOWN (alerted, blocked) — a 409 is never REJECTED on an
    inconclusive lookup, and no second submission is ever attempted."""
    broker = FakeBroker()
    manager = OrderManager(tmp_path / "intents.db")
    calls = {"n": 0}

    def place_raising_409(order):
        calls["n"] += 1
        raise RuntimeError("409 duplicate client_order_id")

    try:
        # Case 1: lookup inconclusive -> UNKNOWN, not REJECTED.
        result = manager.submit(
            _intent(), place_order_fn=place_raising_409,
            lookup_fn=lambda coid: None, sleep_fn=NO_SLEEP, grace_seconds=0,
        )
        assert result["status"] == "UNKNOWN"
        assert calls["n"] == 1  # no resubmit after a conflict
        assert broker.order_count() == 0
        assert manager.get_alerts(result["intent_id"])
        assert manager.is_blocked("AAPL")

        # Case 2: a different symbol, same conflict — the lookup FINDS
        # the existing order -> adopt it instead of rejecting. (A
        # second AAPL intent here would be BLOCKED by case 1's
        # unresolved UNKNOWN, which is the correct behaviour.)
        manager2_intent = _intent(symbol="MSFT", qty=20)
        existing = broker.submit({
            "symbol": "MSFT", "side": "buy", "qty": 20, "order_type": "market",
            "client_order_id": manager2_intent["intent_id"],
        })
        result2 = manager.submit(
            manager2_intent, place_order_fn=place_raising_409,
            lookup_fn=broker.get_by_client_order_id, sleep_fn=NO_SLEEP, grace_seconds=0,
        )
        assert result2["status"] == "SUBMITTED"
        assert result2["broker_order_id"] == existing["id"]
        assert calls["n"] == 2  # one attempt per logical order, no more
        assert broker.order_count() == 1
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


def test_expired_claim_after_attempt_never_resubmits(tmp_path):
    """Lease expiry after the one allowed attempt: a takeover worker
    reconciles only. The second submission attempt must never happen,
    even though the claim has expired and a fresh manager is used."""
    broker = FakeBroker(delayed_visibility_polls=999, drop_next_response=True)
    db = tmp_path / "intents.db"
    calls = {"place": 0}
    now = [1_000_000.0]

    def counting_place(order):
        calls["place"] += 1
        return broker.submit(order)

    manager = OrderManager(db, claim_lease_seconds=0.01, time_fn=lambda: now[0])
    try:
        intent = _intent()
        result = manager.submit(
            intent, place_order_fn=counting_place,
            lookup_fn=broker.get_by_client_order_id,
            sleep_fn=NO_SLEEP, grace_seconds=0, max_polls=3,
        )
        assert result["status"] == "UNKNOWN"
        assert calls["place"] == 1
        assert result["attempts"] == 1
    finally:
        manager.close()

    # Lease long expired + full restart: still exactly one attempt.
    now[0] += 10_000.0
    manager2 = OrderManager(db, claim_lease_seconds=0.01, time_fn=lambda: now[0])
    try:
        again = manager2.submit(
            intent, place_order_fn=counting_place,
            lookup_fn=broker.get_by_client_order_id,
            claimed_by="worker-b", sleep_fn=NO_SLEEP, grace_seconds=0, max_polls=3,
        )
        assert again["status"] == "UNKNOWN"
        assert calls["place"] == 1
        assert broker.order_count() == 1
        assert manager2.is_blocked("AAPL")
    finally:
        manager2.close()
