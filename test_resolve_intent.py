"""B17.1 — operator resolution path for UNKNOWN/BLOCKED intents.

An UNKNOWN exit intent blocks its symbol indefinitely with no admin
path. resolve_intent (order_manager.py) + scripts/resolve_intent.py
give the operator one:
- the broker order is looked up FIRST; if the broker has it, the
  found state is adopted through the normal reconcile path;
- if the lookup is inconclusive, a human decision is required, with a
  reason and a structured evidence reference {source, scope, result};
- a timeout, a dropped response, or repeated 404s alone are
  INSUFFICIENT evidence to mark an order "never accepted" — the
  resolution needs INDEPENDENT broker confirmation (the broker's
  orders list, order history, or a dashboard/support confirmation),
  and the reference must be specific to THIS order (it must name the
  symbol and the client_order_id or broker order id). An empty orders
  list alone — "the orders list came back empty" — is refused and
  the block stays;
- the resolution is an APPENDED record (the resolutions table): it
  never overwrites or erases the intent's attempts, broker_order_id,
  or prior status. The status change is recorded as a human
  resolution, not as if the broker had rejected it.
- without --confirm the script only reports and changes nothing; it
  NEVER submits an order.

Tests (fake clients/fixtures only; the e2e runs main.py as a
subprocess, same harness as B13/B15/B16):
- unit: lookup-found adopts; timeout/404 alone refused; an empty
  orders list alone refused; a scoped evidence reference resolves and
  preserves the attempt history; a BLOCKED intent refuses resolution
  and unblocks after its blocker resolves; a terminal intent refuses;
- CLI: list-only changes nothing; no --confirm changes nothing;
  --check-orders-list resolves; lookup-found adopts; the empty-list
  evidence is refused;
- e2e: the B16 test-3c scenario (UNKNOWN exit intent) -> resolve via
  the script -> the symbol unblocks and the exit is re-derived on the
  next run with a FRESH intent id.
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

from fake_broker import FakeBroker  # noqa: E402
from order_manager import (  # noqa: E402
    OrderManager, ResolveRefusedError,
)
from strategy_runner import _live_intent  # noqa: E402
from test_b13_soak_readiness import (  # noqa: E402
    _latest_decision_log, _read_alerts, _read_intents,
)
from test_b15_sizing_reservations import (  # noqa: E402
    NVDA_FALLING, _broker_state, _env, _open_order_coid, _run_main,
    _write_fixture,
)
from test_b16_cancel_expiry import (  # noqa: E402
    _intent_by_id, _order_count, _reset_submitted_today, _run_buy,
    _strategy_state,
)
from test_b16_done_for_day import (  # noqa: E402
    _held, _stop_coid_for, _stops, _total_sold,
)
from fake_dual_broker import FileBackedFakeBroker  # noqa: E402

VENV_PY = REPO / ".venv" / "bin" / "python"
RESOLVE = REPO / "scripts" / "resolve_intent.py"


# ---------------------------------------------------------------- helpers

def _manager(tmp_path: Path, fake: FakeBroker) -> OrderManager:
    return OrderManager(str(tmp_path / "intents.db"))


def _submit_unknown(mgr: OrderManager, fake: FakeBroker) -> dict:
    """Submit an exit order whose response is dropped: the intent is
    UNKNOWN with one attempt and no broker order id. Returns the
    intent row."""
    order = {
        "symbol": "NVDA", "qty": 2, "side": "sell", "type": "market",
        "time_in_force": "day", "client_order_id": "unused-here",
    }
    intent = _live_intent(order, {
        "symbol": "NVDA", "direction": "mild_down", "decision_score": 15.0,
        "score": 15.0, "trend_quality": 4.5, "last_price": 167.2,
        "target_notional": 334.4, "intended_notional": 334.4,
    }, "2026-11-26")
    fake.drop_next_response = True
    place = lambda o: fake.submit(o)  # noqa: E731
    # The response is dropped AND the submit-time lookup is
    # inconclusive (the broker has not made the order visible yet),
    # so the manager leaves the intent UNKNOWN — exactly the state
    # resolve_intent is for. The real lookup (used at resolve time)
    # finds the order.
    try:
        mgr.submit(intent, place, lookup_fn=lambda _coid: None)
    except TimeoutError:
        pass
    row = mgr.get_intent(intent["intent_id"])
    assert row is not None and row["status"] == "UNKNOWN"
    return row


def _scoped_evidence(intent_id: str) -> dict:
    """A valid independent-broker-confirmation reference for NVDA."""
    return {
        "source": "orders_list",
        "scope": "account orders status=all symbol=NVDA window "
                 "2026-11-25..2026-11-27",
        "result": f"no order with client_order_id {intent_id}",
    }


def _fake_env(home: Path, fixture: Path) -> dict:
    env = _env(home, fixture)
    env["ORDER_STORE_PATH"] = str(home / "order_intents.db")
    env["ATS_FAKE_BROKER_DIR"] = str(fixture)
    return env


# ---------------------------------------------------------------- unit tests

def test_resolve_lookup_found_adopts(tmp_path):
    """The broker HAS the order: resolve_intent adopts it through the
    normal reconcile path, and the resolution record says so."""
    fake = FakeBroker()
    mgr = _manager(tmp_path, fake)
    row = _submit_unknown(mgr, fake)
    assert row["attempts"] == 1 and row["broker_order_id"] is None
    # The order WAS accepted (the response was dropped, not the
    # submission) — the broker's lookup finds it.
    assert fake.get_by_client_order_id(row["intent_id"]) is not None
    outcome, adopted = mgr.resolve_intent(
        row["intent_id"], decision="never_accepted", reason="should not be used",
        evidence=_scoped_evidence(row["intent_id"]), lookup_fn=fake.get_by_client_order_id,
    )
    assert outcome == "adopted_by_lookup"
    assert adopted["status"] == "SUBMITTED"
    assert adopted["broker_order_id"] is not None
    resolutions = mgr.get_resolutions(row["intent_id"])
    assert len(resolutions) == 1
    assert resolutions[0]["decision"] == "adopted_by_lookup"
    assert resolutions[0]["attempts_at_resolution"] == 1


def test_resolve_timeout_alone_refused(tmp_path):
    """Only a timeout/404 history and no independent confirmation:
    refused, the block stays."""
    fake = FakeBroker()
    mgr = _manager(tmp_path, fake)
    row = _submit_unknown(mgr, fake)
    with pytest.raises(ResolveRefusedError):
        mgr.resolve_intent(
            row["intent_id"], decision="never_accepted",
            reason="the lookup kept timing out, so it must never have been accepted",
            evidence=None, lookup_fn=lambda _coid: None,
        )
    assert mgr.get_intent(row["intent_id"])["status"] == "UNKNOWN"
    assert mgr.get_resolutions(row["intent_id"]) == []


def test_resolve_bare_string_evidence_refused(tmp_path):
    fake = FakeBroker()
    mgr = _manager(tmp_path, fake)
    row = _submit_unknown(mgr, fake)
    with pytest.raises(ResolveRefusedError):
        mgr.resolve_intent(
            row["intent_id"], decision="never_accepted",
            reason="the orders list came back empty",
            evidence="the orders list came back empty",
            lookup_fn=lambda _coid: None,
        )
    assert mgr.get_intent(row["intent_id"])["status"] == "UNKNOWN"


def test_resolve_empty_orders_list_alone_refused(tmp_path):
    """DETAIL 1: an empty orders list ALONE is insufficient — the
    reference must name the symbol and the order. This evidence says
    only that a list came back empty; the block stays."""
    fake = FakeBroker()
    mgr = _manager(tmp_path, fake)
    row = _submit_unknown(mgr, fake)
    with pytest.raises(ResolveRefusedError):
        mgr.resolve_intent(
            row["intent_id"], decision="never_accepted",
            reason="the orders list came back empty",
            evidence={
                "source": "orders_list",
                "scope": "orders list came back empty",
                "result": "empty",
            },
            lookup_fn=lambda _coid: None,
        )
    assert mgr.get_intent(row["intent_id"])["status"] == "UNKNOWN"
    assert mgr.get_resolutions(row["intent_id"]) == []


def test_resolve_scoped_evidence_resolves_and_preserves_history(tmp_path):
    """A resolution with a scoped evidence reference resolves, and
    the original attempt history is PRESERVED (appended record; the
    intent's attempts and broker_order_id are untouched)."""
    fake = FakeBroker()
    mgr = _manager(tmp_path, fake)
    row = _submit_unknown(mgr, fake)
    outcome, resolved = mgr.resolve_intent(
        row["intent_id"], decision="never_accepted",
        reason="Alpaca order history shows no such order",
        evidence=_scoped_evidence(row["intent_id"]),
        lookup_fn=lambda _coid: None,
    )
    assert outcome == "resolved"
    assert resolved["status"] == "REJECTED"
    # Attempt history preserved: 1 attempt, broker_order_id still None.
    assert resolved["attempts"] == 1
    assert resolved["broker_order_id"] is None
    assert resolved["resolved_by"] == "operator"
    assert resolved["resolution_reason"] == "Alpaca order history shows no such order"
    resolutions = mgr.get_resolutions(row["intent_id"])
    assert len(resolutions) == 1
    record = resolutions[0]
    assert record["decision"] == "never_accepted"
    assert record["previous_status"] == "UNKNOWN"
    assert record["new_status"] == "REJECTED"
    assert record["attempts_at_resolution"] == 1
    evidence = json.loads(record["evidence"])
    assert evidence["source"] == "orders_list"
    assert "NVDA" in evidence["scope"]
    assert row["intent_id"] in evidence["result"]


def test_resolve_accepted_adopts_supplied_state(tmp_path):
    fake = FakeBroker()
    mgr = _manager(tmp_path, fake)
    row = _submit_unknown(mgr, fake)
    outcome, resolved = mgr.resolve_intent(
        row["intent_id"], decision="accepted",
        reason="Alpaca dashboard shows the order filled 2 @ 167.25",
        evidence={
            "source": "dashboard",
            "scope": f"account orders NVDA order {row['intent_id']}",
            "result": "filled 2 @ 167.25",
        },
        status="filled", filled_qty=2.0, lookup_fn=lambda _coid: None,
    )
    assert outcome == "resolved"
    assert resolved["status"] == "FILLED"
    assert resolved["filled_qty"] == 2.0
    assert resolved["attempts"] == 1  # history preserved


def test_resolve_accepted_requires_status_and_qty(tmp_path):
    fake = FakeBroker()
    mgr = _manager(tmp_path, fake)
    row = _submit_unknown(mgr, fake)
    with pytest.raises(ResolveRefusedError):
        mgr.resolve_intent(
            row["intent_id"], decision="accepted",
            reason="the broker says it was accepted",
            evidence=_scoped_evidence(row["intent_id"]),
            lookup_fn=lambda _coid: None,
        )
    assert mgr.get_intent(row["intent_id"])["status"] == "UNKNOWN"


def test_resolve_blocked_intent_refused(tmp_path):
    """A BLOCKED intent was never submitted; it cannot be resolved —
    resolve its blocker instead."""
    fake = FakeBroker()
    mgr = _manager(tmp_path, fake)
    blocker = _submit_unknown(mgr, fake)
    order = {
        "symbol": "NVDA", "qty": 2, "side": "sell", "type": "market",
        "time_in_force": "day", "client_order_id": "unused-here",
    }
    intent = _live_intent(order, {
        "symbol": "NVDA", "direction": "mild_down", "decision_score": 15.0,
        "score": 15.0, "trend_quality": 4.5, "last_price": 167.2,
        "target_notional": 334.4, "intended_notional": 334.4,
        "exit_attempt": 1,  # a second, distinct intent
    }, "2026-11-26")
    mgr.submit(intent, lambda o: fake.submit(o),
               lookup_fn=fake.get_by_client_order_id)
    blocked = mgr.get_intent(intent["intent_id"])
    assert blocked["status"] == "BLOCKED"
    with pytest.raises(ResolveRefusedError):
        mgr.resolve_intent(
            blocked["intent_id"], decision="never_accepted",
            reason="the blocker is gone, so this should be resolved too",
            evidence=_scoped_evidence(blocked["intent_id"]),
            lookup_fn=lambda _coid: None,
        )
    assert mgr.get_intent(blocked["intent_id"])["status"] == "BLOCKED"


def test_blocked_intent_unblocks_after_blocker_resolves(tmp_path):
    """A BLOCKED intent unblocks after its blocker resolves: the
    same order can then be submitted and accepted."""
    fake = FakeBroker()
    mgr = _manager(tmp_path, fake)
    blocker = _submit_unknown(mgr, fake)
    order = {
        "symbol": "NVDA", "qty": 2, "side": "sell", "type": "market",
        "time_in_force": "day", "client_order_id": "unused-here",
    }
    intent = _live_intent(order, {
        "symbol": "NVDA", "direction": "mild_down", "decision_score": 15.0,
        "score": 15.0, "trend_quality": 4.5, "last_price": 167.2,
        "target_notional": 334.4, "intended_notional": 334.4,
        "exit_attempt": 1,  # a second, distinct intent
    }, "2026-11-26")
    mgr.submit(intent, lambda o: fake.submit(o),
               lookup_fn=fake.get_by_client_order_id)
    assert mgr.get_intent(intent["intent_id"])["status"] == "BLOCKED"
    mgr.resolve_intent(
        blocker["intent_id"], decision="never_accepted",
        reason="Alpaca order history shows no such order",
        evidence=_scoped_evidence(blocker["intent_id"]),
        lookup_fn=lambda _coid: None,
    )
    assert mgr.get_intent(blocker["intent_id"])["status"] == "REJECTED"
    outcome = mgr.submit(intent, lambda o: fake.submit(o),
                         lookup_fn=fake.get_by_client_order_id)
    assert outcome["status"] == "SUBMITTED"
    # Two broker orders: the blocker (accepted, response dropped) and
    # the unblocked intent's own order — no duplicate of either.
    assert fake.order_count() == 2
    assert outcome["broker_order_id"] != blocker["broker_order_id"]


def test_resolve_terminal_intent_refused(tmp_path):
    fake = FakeBroker()
    mgr = _manager(tmp_path, fake)
    order = {
        "symbol": "NVDA", "qty": 2, "side": "buy", "type": "limit",
        "limit_price": 167.25, "time_in_force": "day",
        "client_order_id": "unused-here",
    }
    intent = _live_intent(order, {
        "symbol": "NVDA", "direction": "mild_up", "decision_score": 15.0,
        "score": 15.0, "trend_quality": 4.5, "last_price": 167.2,
        "target_notional": 334.4, "intended_notional": 334.4,
    }, "2026-11-26")
    mgr.submit(intent, lambda o: fake.submit(o),
               lookup_fn=fake.get_by_client_order_id)
    # Cancel the broker order and reconcile: the intent becomes
    # CANCELLED (terminal) through the normal reconcile path.
    submitted = mgr.get_intent(intent["intent_id"])
    # cancel takes the CLIENT order id (the intent id), not the
    # broker's fake-0001 id.
    fake.cancel(submitted["intent_id"])
    # reconcile() is for UNKNOWN intents; a SUBMITTED intent is
    # synced by reconcile_open_intents.
    mgr.reconcile_open_intents(lookup_fn=fake.get_by_client_order_id)
    assert mgr.get_intent(intent["intent_id"])["status"] == "CANCELLED"
    with pytest.raises(ResolveRefusedError):
        mgr.resolve_intent(
            intent["intent_id"], decision="never_accepted",
            reason="should not matter", evidence=_scoped_evidence(intent["intent_id"]),
            lookup_fn=lambda _coid: None,
        )


def test_unresolved_intents_lists_unknown_and_blocked(tmp_path):
    fake = FakeBroker()
    mgr = _manager(tmp_path, fake)
    blocker = _submit_unknown(mgr, fake)
    rows = mgr.unresolved_intents()
    assert [r["intent_id"] for r in rows] == [blocker["intent_id"]]
    assert rows[0]["symbol"] == "NVDA" and rows[0]["attempts"] == 1


# ---------------------------------------------------------------- CLI tests

def _setup_unknown_exit(tmp_path) -> tuple[Path, Path, dict]:
    """Run the B16 test-3c scenario up to the UNKNOWN exit intent.
    Returns (home, fixture, exit_intent)."""
    home = tmp_path / "home"
    fixture = tmp_path / "fixture"
    home.mkdir(parents=True)
    _, coid, limit = _run_buy(home, fixture, exit_on_signal_loss=1)
    store = FileBackedFakeBroker(fixture)
    store.fill(coid, "fill-1", 2, limit)
    _reset_submitted_today(home)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"],
                        exit_on_signal_loss=1)
    assert rc == 0, out
    assert _latest_decision_log(home)["status"] == "ok", out
    stop_coid = _stop_coid_for(fixture, "NVDA")
    assert _stops(fixture)[stop_coid]["qty"] == 2.0
    store = FileBackedFakeBroker(fixture)
    store.fail_next_submit()
    _write_fixture(fixture, nvda_cycle=NVDA_FALLING)
    _reset_submitted_today(home)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"],
                        exit_on_signal_loss=1)
    assert rc == 0, out
    log2 = _latest_decision_log(home)
    assert log2["status"] == "ok", out
    assert _order_count(fixture) == 1  # no sell submitted
    exit_intent = next(
        i for i in _read_intents(home)
        if i["symbol"] == "NVDA" and i["status"] == "UNKNOWN"
        and i["side"] == "sell"
    )
    return home, fixture, exit_intent


def test_cli_list_only_changes_nothing(tmp_path):
    home, fixture, exit_intent = _setup_unknown_exit(tmp_path)
    env = _fake_env(home, fixture)
    proc = subprocess.run(
        [str(VENV_PY), str(RESOLVE), "--store", str(home / "order_intents.db"),
         "--broker", "fake"],
        capture_output=True, text=True, cwd=str(REPO), env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert exit_intent["intent_id"] in proc.stdout
    assert "UNKNOWN" in proc.stdout
    assert _intent_by_id(home, exit_intent["intent_id"])["status"] == "UNKNOWN"


def test_cli_no_confirm_changes_nothing(tmp_path):
    home, fixture, exit_intent = _setup_unknown_exit(tmp_path)
    env = _fake_env(home, fixture)
    proc = subprocess.run(
        [str(VENV_PY), str(RESOLVE), "--store", str(home / "order_intents.db"),
         "--broker", "fake",
         "--intent-id", exit_intent["intent_id"],
         "--decision", "never_accepted",
         "--reason", "Alpaca order history shows no such order",
         "--check-orders-list"],
        capture_output=True, text=True, cwd=str(REPO), env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "No --confirm" in proc.stdout
    assert _intent_by_id(home, exit_intent["intent_id"])["status"] == "UNKNOWN"


def test_cli_check_orders_list_resolves(tmp_path):
    """--check-orders-list builds the scoped evidence from the
    broker's own full order history: the exit (never accepted — the
    submit failed before storing) is not in it, so the resolution
    proceeds and the attempt history is preserved."""
    home, fixture, exit_intent = _setup_unknown_exit(tmp_path)
    env = _fake_env(home, fixture)
    proc = subprocess.run(
        [str(VENV_PY), str(RESOLVE), "--store", str(home / "order_intents.db"),
         "--broker", "fake",
         "--intent-id", exit_intent["intent_id"],
         "--decision", "never_accepted",
         "--reason", "Alpaca order history shows no such order",
         "--check-orders-list", "--confirm"],
        capture_output=True, text=True, cwd=str(REPO), env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "REJECTED" in proc.stdout
    resolved = OrderManager(str(home / "order_intents.db")).get_intent(exit_intent["intent_id"])
    assert resolved["status"] == "REJECTED"
    assert resolved["attempts"] == 1  # history preserved
    assert resolved["broker_order_id"] is None
    assert resolved["resolved_by"] == "operator"
    mgr = OrderManager(str(home / "order_intents.db"))
    records = mgr.get_resolutions(exit_intent["intent_id"])
    assert len(records) == 1
    evidence = json.loads(records[0]["evidence"])
    assert evidence["source"] == "orders_list"
    assert "NVDA" in evidence["scope"]
    assert exit_intent["intent_id"] in evidence["result"]


def test_cli_empty_list_evidence_refused(tmp_path):
    home, fixture, exit_intent = _setup_unknown_exit(tmp_path)
    env = _fake_env(home, fixture)
    proc = subprocess.run(
        [str(VENV_PY), str(RESOLVE), "--store", str(home / "order_intents.db"),
         "--broker", "fake",
         "--intent-id", exit_intent["intent_id"],
         "--decision", "never_accepted",
         "--reason", "the orders list came back empty",
         "--evidence", json.dumps({
             "source": "orders_list",
             "scope": "orders list came back empty",
             "result": "empty",
         }),
         "--confirm"],
        capture_output=True, text=True, cwd=str(REPO), env=env,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "refused" in (proc.stdout + proc.stderr).lower()
    assert _intent_by_id(home, exit_intent["intent_id"])["status"] == "UNKNOWN"


def test_cli_lookup_found_adopts(tmp_path):
    """The broker HAS the order (accepted, response dropped, submit-time
    lookup inconclusive): the script's lookup-first path adopts it
    through the normal reconcile path."""
    home = tmp_path / "home"
    fixture = tmp_path / "fixture"
    home.mkdir(parents=True)
    _write_fixture(fixture)
    store = FileBackedFakeBroker(fixture)
    order = {
        "symbol": "NVDA", "qty": 2, "side": "sell", "type": "market",
        "time_in_force": "day", "client_order_id": "lookup-adopts-1",
    }
    intent = _live_intent(order, {
        "symbol": "NVDA", "direction": "mild_down", "decision_score": 15.0,
        "score": 15.0, "trend_quality": 4.5, "last_price": 167.2,
        "target_notional": 334.4, "intended_notional": 334.4,
    }, "2026-11-26")
    # place stores the order (client_order_id = the intent id), then
    # the response is dropped; the submit-time lookup is inconclusive,
    # so the intent is UNKNOWN while the broker holds the order.
    def place_then_drop(o):
        store.submit(o)
        raise TimeoutError("response dropped")
    mgr = OrderManager(str(home / "order_intents.db"))
    # The manager swallows the dropped response (it polls the lookup,
    # which is inconclusive) and leaves the intent UNKNOWN.
    mgr.submit(intent, place_then_drop, lookup_fn=lambda _coid: None)
    assert mgr.get_intent(intent["intent_id"])["status"] == "UNKNOWN"
    assert _broker_state(fixture)["orders"][intent["intent_id"]]["status"] == "submitted"
    env = _fake_env(home, fixture)
    proc = subprocess.run(
        [str(VENV_PY), str(RESOLVE), "--store", str(home / "order_intents.db"),
         "--broker", "fake",
         "--intent-id", intent["intent_id"], "--confirm"],
        capture_output=True, text=True, cwd=str(REPO), env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "adopted" in proc.stdout.lower()
    adopted = _intent_by_id(home, intent["intent_id"])
    assert adopted["status"] == "SUBMITTED"
    assert adopted["attempts"] == 1


# ---------------------------------------------------------------- e2e

def test_e2e_unknown_exit_resolved_then_rederived(tmp_path):
    """B16 test-3c, then the operator resolves the UNKNOWN exit via
    the script, and the next run re-derives the exit with a FRESH
    intent id (the original intent stays REJECTED, its history
    preserved) and submits it."""
    home, fixture, exit_intent = _setup_unknown_exit(tmp_path)
    original_id = exit_intent["intent_id"]
    env = _fake_env(home, fixture)
    proc = subprocess.run(
        [str(VENV_PY), str(RESOLVE), "--store", str(home / "order_intents.db"),
         "--broker", "fake",
         "--intent-id", original_id,
         "--decision", "never_accepted",
         "--reason", "Alpaca order history shows no such order",
         "--check-orders-list", "--confirm"],
        capture_output=True, text=True, cwd=str(REPO), env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _intent_by_id(home, original_id)["status"] == "REJECTED"
    # Run 3: the symbol unblocks; the exit is re-derived with a fresh
    # intent id (exit_attempt=1) and submitted (the fail flag was
    # consumed by Run 2, so the submit succeeds).
    _reset_submitted_today(home)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"],
                        exit_on_signal_loss=1)
    assert rc == 0, out
    log3 = _latest_decision_log(home)
    assert log3["status"] == "ok", out
    assert _order_count(fixture) == 2  # buy + the re-derived exit
    intents = _read_intents(home)
    original = _intent_by_id(home, original_id)
    assert original["status"] == "REJECTED"
    assert original["attempts"] == 1
    new_exit = next(
        i for i in intents
        if i["symbol"] == "NVDA" and i["status"] == "SUBMITTED"
        and i["side"] == "sell"
    )
    assert new_exit["intent_id"] != original_id
    assert new_exit["attempts"] == 1
    assert new_exit["broker_order_id"] is not None
    assert _strategy_state(home)["open_entries"] == {}
    # No duplicate exit: the re-derived exit is the only sell.
    sells = [i for i in intents
             if i["symbol"] == "NVDA" and i["side"] == "sell"]
    assert len(sells) == 2  # the rejected original + the re-derived one
