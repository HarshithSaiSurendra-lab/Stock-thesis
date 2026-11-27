"""B18 — the remaining exposure from the B17 report.

Covers B18.1 (entry intent-id collision: a same-day re-entry after
a close reuses the old FILLED intent id), B18.2 (live-leg stop
sizing, paper-only before), B18.3 (live-leg oversell check,
paper-only before), B18.4 (pending_exit_filled snapshot blemish)
and B18.5 (close decisions at fill, not at submission).

OCO/bracket orders are DEFERRED (user decision, Nov 27, 2026):
nothing here changes entry order types. B17.4/B18.3 are DETECTION +
halt, not prevention — the oversell has already happened by the time
they fire; the halt and alert stay regardless.

All tests use fake clients/fixtures only; the e2e runs main.py as a
subprocess, same harness as B13/B15/B16/B17. No real or paper orders.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

from fake_dual_broker import FakeDualBroker, FileBackedFakeBroker  # noqa: E402
from memory import TradeMemory  # noqa: E402
from strategy_runner import (  # noqa: E402
    StrategyState, _close_pending_exit_decision, _entry_intent_inputs,
    _live_intent, _mirror_cancelled_intents_into_state,
    _mirror_stop_fills_at_startup, _stop_leg_attribution,
)
from test_b13_soak_readiness import (  # noqa: E402
    _latest_decision_log, _read_alerts, _read_intents,
)
from test_b15_sizing_reservations import (  # noqa: E402
    NVDA_FALLING, _broker_state, _open_order_coid, _run_main,
    _write_fixture,
)
from test_b16_cancel_expiry import (  # noqa: E402
    _order_count, _reset_submitted_today, _run_buy, _strategy_state,
)
from test_b16_done_for_day import (  # noqa: E402
    _alerts_log, _held, _stop_coid_for, _stops, _total_sold,
)
from test_b17_remaining_exposure import (  # noqa: E402
    _buy_fill_resize, _decision, _decisions, _fill_buy, _guardian_stub,
    _live_order_count, _live_store, _mirror_state, _protection,
    _run_exit, _set_live_enabled, _stop_attribution, _stop_sync,
    _tax_lots,
)

VENV_PY = REPO / ".venv" / "bin" / "python"


# ---------------------------------------------------------------- helpers

def _live_broker_state(fixture_dir: Path) -> dict:
    return json.loads((fixture_dir / "broker_state_live.json").read_text())


def _live_stops(fixture_dir: Path) -> dict:
    return _live_broker_state(fixture_dir).get("stops", {})


def _live_total_sold(fixture_dir: Path, symbol: str) -> float:
    """Shares actually sold on the LIVE leg (strategy orders + stops)."""
    state = _live_broker_state(fixture_dir)
    total = 0.0
    for orders in (state.get("orders", {}), state.get("stops", {})):
        for o in orders.values():
            if o.get("symbol") == symbol and str(o.get("side", "")).lower() == "sell":
                total += float(o.get("filled_qty") or 0.0)
    return total


def _live_held(fixture_dir: Path, symbol: str) -> float:
    """Net filled qty on the LIVE leg (buys + / sells -)."""
    state = _live_broker_state(fixture_dir)
    total = 0.0
    for orders in (state.get("orders", {}), state.get("stops", {})):
        for o in orders.values():
            if o.get("symbol") == symbol:
                qty = float(o.get("filled_qty") or 0.0)
                total += qty if str(o.get("side", "")).lower() == "buy" else -qty
    return total


def _stop_sync_leg(log: dict, action: str, account: str) -> dict | None:
    for e in log.get("stop_sync") or []:
        if e.get("action") == action and e.get("account") == account:
            return e
    return None


def _reset_order_rate(home: Path) -> None:
    """Clear the guardian's 60-second order-rate window (test-only).

    guardian.submit records EVERY invocation, including the
    disabled-live-leg no-op — so three submit runs in the test
    harness record 5 orders in <60s, and the 5/min guard halts the
    guardian on the next submit (the live no-op in the third run).
    In production the daily job runs once per day, so the window is
    always empty at run start and the guard never fires; the test
    compresses runs into seconds. Clearing recent_order_times
    simulates that real spacing. A halt whose reason is exactly
    the rate limit is cleared too — it is the same artifact (the
    halt fired only because the window was full); any OTHER halt
    reason (kill switch, daily loss, stale data) is left alone so
    a real safety halt still fails the test loudly. orders_today
    is left untouched."""
    path = home / "guardian_state.json"
    if not path.exists():
        return
    state = json.loads(path.read_text())
    state["recent_order_times"] = []
    if state.get("halt_reason") == "order_rate_exceeded":
        state["halted"] = False
        state["halt_reason"] = None
    path.write_text(json.dumps(state))


def _buy_fill_resize_live(home: Path, fixture: Path) -> tuple[float, float]:
    """Live-enabled _buy_fill_resize: Run 1 buys NVDA qty 2 on BOTH
    legs (unfilled), both buys fill 2, and the next run's startup
    size sync attaches a stop sized to the filled qty (2) on BOTH
    legs. Returns (limit, entry_price)."""
    _write_fixture(fixture)
    _set_live_enabled(fixture)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"],
                        exit_on_signal_loss=1)
    assert rc == 0, out
    log1 = _latest_decision_log(home)
    assert log1["status"] == "ok", out
    limit = next(o for o in log1["orders"]
                 if o["symbol"] == "NVDA")["order"]["limit_price"]
    paper_store = FileBackedFakeBroker(fixture)
    paper_store.fill(_open_order_coid(fixture, "NVDA"), "fill-1", 2, limit)
    live_store = _live_store(fixture)
    live_orders = live_store.list_all_orders()
    assert len(live_orders) == 1
    live_store.fill(live_orders[0]["client_order_id"], "fill-live-1", 2, limit)
    _reset_submitted_today(home)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"],
                        exit_on_signal_loss=1)
    assert rc == 0, out
    log = _latest_decision_log(home)
    assert log["status"] == "ok", out
    assert _stops(fixture)[_stop_coid_for(fixture, "NVDA")]["qty"] == 2.0
    live_stops = _live_stops(fixture)
    assert len(live_stops) == 1
    assert next(iter(live_stops.values()))["qty"] == 2.0
    entry_price = _strategy_state(home)["open_entries"]["NVDA"]["entry_price"]
    return limit, entry_price


# ---------------------------------------------------------------- B18.1 unit

_ENTRY_ITEM = {
    "symbol": "NVDA", "direction": "mild_up",
    "decision_score": 12.3456789, "score": 0.123456789,
    "trend_quality": 0.987654321,
}
_ENTRY_ORDER = {"symbol": "NVDA", "side": "buy", "qty": 2.0,
                "order_type": "limit", "limit_price": 167.25}


def _pre_b18_entry_inputs() -> dict:
    """The entry intent inputs exactly as the entry loop built them
    before B18 (no attempt component)."""
    return {
        "symbol": "NVDA",
        "direction": "mild_up",
        "decision_score": round(12.3456789, 6),
        "score": round(0.123456789, 6),
        "trend_quality": round(0.987654321, 6),
        "last_price": round(167.25, 2),
        "target_notional": round(334.52, 2),
        "intended_notional": round(334.52, 2),
    }


def test_entry_intent_first_attempt_equals_pre_b18_id():
    """B18.1: the first attempt omits "entry_attempt", so its intent
    id equals the pre-B18 id exactly — existing ids are unchanged."""
    inputs = _entry_intent_inputs(_ENTRY_ITEM, 167.25, 334.52, 334.52, 0)
    assert "entry_attempt" not in inputs
    assert inputs == _pre_b18_entry_inputs()
    first_id = _live_intent(_ENTRY_ORDER, inputs, "2026-11-25")["intent_id"]
    old_id = _live_intent(_ENTRY_ORDER, _pre_b18_entry_inputs(), "2026-11-25")["intent_id"]
    assert first_id == old_id


def test_entry_intent_attempt_changes_id_and_rederives():
    """B18.1: the same decision at attempt 1 gets a DIFFERENT intent
    id; re-deriving at the same attempt reuses the same id
    (exact-once across restarts); attempt 2 differs again."""
    id0 = _live_intent(
        _ENTRY_ORDER, _entry_intent_inputs(_ENTRY_ITEM, 167.25, 334.52, 334.52, 0),
        "2026-11-25")["intent_id"]
    id1 = _live_intent(
        _ENTRY_ORDER, _entry_intent_inputs(_ENTRY_ITEM, 167.25, 334.52, 334.52, 1),
        "2026-11-25")["intent_id"]
    id1_again = _live_intent(
        _ENTRY_ORDER, _entry_intent_inputs(_ENTRY_ITEM, 167.25, 334.52, 334.52, 1),
        "2026-11-25")["intent_id"]
    id2 = _live_intent(
        _ENTRY_ORDER, _entry_intent_inputs(_ENTRY_ITEM, 167.25, 334.52, 334.52, 2),
        "2026-11-25")["intent_id"]
    assert id1 != id0
    assert id1_again == id1
    assert id2 != id1 and id2 != id0


def test_entry_intent_would_collide_without_attempt():
    """B18.1: the pre-fix shape — deriving the same decision twice
    with NO attempt component yields the SAME intent id. That is the
    collision: a same-day re-entry after a close re-derives the old
    FILLED entry intent and the manager returns it, a phantom
    position. (Proven here by derivation, not by reintroducing the
    bug.)"""
    id_a = _live_intent(_ENTRY_ORDER, _pre_b18_entry_inputs(), "2026-11-25")["intent_id"]
    id_b = _live_intent(_ENTRY_ORDER, _pre_b18_entry_inputs(), "2026-11-25")["intent_id"]
    assert id_a == id_b


def test_mirror_cancelled_removal_bumps_entry_attempts():
    """B18.1 (unit): when the cancelled-intent mirror REMOVES the
    entry (0 filled), the entry attempt bumps to 1. When it only
    REDUCES the entry (shares filled — the trade continues), it does
    NOT bump."""
    state = StrategyState()
    state.open_entries["NVDA"] = {
        "symbol": "NVDA", "qty": 2.0, "side": "buy", "intent_id": "buy-1",
        "decision_id": 1, "entry_price": 167.25,
    }
    state.expected_positions["paper"]["NVDA"] = {"qty": 2.0, "decision_id": 1}
    removed = _mirror_cancelled_intents_into_state(state, [{
        "intent_id": "buy-1", "symbol": "NVDA", "account": "paper",
        "status": "CANCELLED", "filled_qty": 0.0}])
    assert removed and removed[0]["action"] == "removed"
    assert state.open_entries == {}
    assert state.entry_attempts == {"NVDA": 1}

    state.open_entries["NVDA"] = {
        "symbol": "NVDA", "qty": 2.0, "side": "buy", "intent_id": "buy-2",
        "decision_id": 2, "entry_price": 167.25,
    }
    state.expected_positions["paper"]["NVDA"] = {"qty": 2.0, "decision_id": 2}
    reduced = _mirror_cancelled_intents_into_state(state, [{
        "intent_id": "buy-2", "symbol": "NVDA", "account": "paper",
        "status": "CANCELLED", "filled_qty": 1.0}])
    assert reduced and reduced[0]["action"] == "reduced_to_filled"
    assert state.open_entries["NVDA"]["qty"] == 1.0
    assert state.entry_attempts == {"NVDA": 1}  # no bump: shares held


def test_mirror_stop_fill_removal_bumps_entry_attempts(tmp_path):
    """B18.1 (unit): when the stop-fill mirror REMOVES the entry
    (the stop sold everything), the entry attempt bumps to 1. When
    it only REDUCES the entry (a partial stop fill), it does NOT
    bump."""
    store, broker, guardian, state, memory, decision_id, lot_id = _mirror_state(
        tmp_path, qty=2.0)
    _fill_buy(store, 2.0, 167.25)
    stop = store.attach_trailing_stop("NVDA", 2.0, 0.06)
    store.trigger_trailing_stop(stop["client_order_id"], 160.00)
    state.open_entries["NVDA"]["trail_stop_id"] = stop["id"]
    state.open_entries["NVDA"]["trail_stop_coid"] = stop["client_order_id"]
    broker = FakeDualBroker(tmp_path / "fixture")
    synced = _mirror_stop_fills_at_startup(broker, guardian, state, memory=memory)
    assert state.open_entries == {}
    assert state.entry_attempts == {"NVDA": 1}
    assert _stop_sync({"stop_sync": synced}, "stop_fill_mirrored_removed") is not None

    # A fresh memory db for the partial case (each setup inserts
    # decision 1).
    part2 = tmp_path / "part2"
    part2.mkdir()
    store, broker, guardian, state, memory, decision_id, lot_id = _mirror_state(
        part2, qty=2.0)
    _fill_buy(store, 2.0, 167.25)
    stop = store.attach_trailing_stop("NVDA", 2.0, 0.06)
    store.fill(stop["client_order_id"], "stop-fill-1", 1, 160.00)
    state.open_entries["NVDA"]["trail_stop_id"] = stop["id"]
    state.open_entries["NVDA"]["trail_stop_coid"] = stop["client_order_id"]
    broker = FakeDualBroker(part2 / "fixture")
    synced = _mirror_stop_fills_at_startup(broker, guardian, state, memory=memory)
    assert state.open_entries["NVDA"]["qty"] == 1.0
    assert state.entry_attempts == {}  # no bump: shares held


def test_stop_leg_attribution_splits_by_account():
    """B18.3/B18.5: _stop_leg_attribution splits the combined fills
    by their "account" tag (an untagged fill counts as paper)."""
    fills = [
        {"client_order_id": "paper-trail", "filled_qty": 1.0,
         "filled_avg_price": 160.00, "account": "paper"},
        {"client_order_id": "live-trail", "filled_qty": 2.0,
         "filled_avg_price": 150.00, "account": "live"},
        {"client_order_id": "old-trail", "filled_qty": 1.0,
         "filled_avg_price": 170.00},  # no tag -> paper
    ]
    sold, price, pl = _stop_leg_attribution(fills, "paper", 167.25)
    assert sold == 2.0
    assert price == (160.00 * 1.0 + 170.00 * 1.0) / 2.0
    assert pl == (price - 167.25) * 2.0
    sold, price, pl = _stop_leg_attribution(fills, "live", 167.25)
    assert sold == 2.0
    assert price == 150.00
    assert pl == (150.00 - 167.25) * 2.0
    # A leg that sold shares with no fetchable price: pl unknown (None).
    sold, price, pl = _stop_leg_attribution(
        [{"client_order_id": "x", "filled_qty": 1.0,
          "filled_avg_price": None, "account": "live"}], "live", 167.25)
    assert sold == 1.0 and price is None and pl is None
    # A leg that sold nothing: pl 0.0.
    sold, price, pl = _stop_leg_attribution([], "live", 167.25)
    assert sold == 0.0 and price is None and pl == 0.0


# ---------------------------------------------------------------- B18.1 e2e

def test_entry_reentry_gets_fresh_intent_id(tmp_path):
    """B18.1 e2e: Run 1 buys NVDA qty 2 and it fills; Run 2 exits on
    signal loss and the exit fills; Run 3 (submitted_today reset,
    fixture back to an uptrend) re-derives the same buy — a FRESH
    intent id, a second broker order (order_count 3: buy + exit +
    new buy), status ok, no drift halt, expected position 2. The
    pre-fix shape would have re-derived the old FILLED intent id
    and halted on the phantom position (expected 2, actual 0)."""
    home = tmp_path / "home"
    fixture = tmp_path / "fixture"
    home.mkdir(parents=True)
    _, buy_coid, limit = _run_buy(home, fixture, exit_on_signal_loss=1)
    store = FileBackedFakeBroker(fixture)
    store.fill(buy_coid, "fill-1", 2, limit)
    entry_price = _strategy_state(home)["open_entries"]["NVDA"]["entry_price"]
    log2 = _run_exit(home, fixture)
    assert _order_count(fixture) == 2  # buy + exit
    sell = next(
        i for i in _read_intents(home)
        if i["symbol"] == "NVDA" and i["side"] == "sell")
    assert sell["qty"] == 2.0 and sell["status"] == "SUBMITTED"
    # The trade ended: the entry attempt bumped (persisted in state).
    assert _strategy_state(home)["entry_attempts"] == {"NVDA": 1}
    # The decision is still open (B18.5: the exit is unfilled).
    assert _decision(home, 1)["closed"] == 0
    store = FileBackedFakeBroker(fixture)
    store.fill(sell["intent_id"], "fill-exit-1", 2, 160.00)
    assert _total_sold(fixture, "NVDA") == 2.0
    assert _held(fixture, "NVDA") == 0.0
    # Run 3: same trading day, uptrend again — the symbol is
    # re-selected as a buy.
    _write_fixture(fixture)
    _reset_submitted_today(home)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"],
                        exit_on_signal_loss=1)
    assert rc == 0, out
    log3 = _latest_decision_log(home)
    assert log3["status"] == "ok", out
    assert _order_count(fixture) == 3  # buy + exit + NEW buy
    new_buy_coid = _open_order_coid(fixture, "NVDA")
    assert new_buy_coid != buy_coid  # a FRESH intent id, not the old FILLED one
    buys = [i for i in _read_intents(home)
            if i["symbol"] == "NVDA" and i["side"] == "buy"]
    assert len(buys) == 2
    assert buys[0]["status"] == "FILLED" and buys[1]["status"] == "SUBMITTED"
    assert buys[1]["intent_id"] == new_buy_coid
    # No drift halt: the re-derived buy's expected position is 2
    # (the reconcile counts the 2 open shares toward actual).
    assert _strategy_state(home)["expected_positions"]["paper"]["NVDA"]["qty"] == 2.0
    # The exit's fill closed the decision in Run 3's startup sync.
    assert _decision(home, 1)["closed"] == 1
    assert _decision(home, 1)["exit_price"] == 160.00
    assert _decision(home, 1)["realized_pl"] == (160.00 - entry_price) * 2


# ---------------------------------------------------------------- B18.2 e2e

def test_live_stop_sized_to_live_filled_qty(tmp_path):
    """B18.2 e2e (live enabled): the live buy partially fills (1 of
    2) — the live stop attaches at the filled qty only (1, not 2);
    the 2nd live share fills — the old live stop is cancelled BY
    ORDER ID and a new live stop attaches at the new filled qty (2).
    Per-step sellable never exceeds live held."""
    home = tmp_path / "home"
    fixture = tmp_path / "fixture"
    home.mkdir(parents=True)
    _write_fixture(fixture)
    _set_live_enabled(fixture)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"],
                        exit_on_signal_loss=1)
    assert rc == 0, out
    log1 = _latest_decision_log(home)
    assert log1["status"] == "ok", out
    limit = next(o for o in log1["orders"]
                 if o["symbol"] == "NVDA")["order"]["limit_price"]
    # Partial fills on BOTH legs: 1 of 2.
    paper_store = FileBackedFakeBroker(fixture)
    paper_store.fill(_open_order_coid(fixture, "NVDA"), "fill-1", 1, limit)
    live_store = _live_store(fixture)
    live_orders = live_store.list_all_orders()
    assert len(live_orders) == 1
    live_store.fill(live_orders[0]["client_order_id"], "fill-live-1", 1, limit)
    _reset_submitted_today(home)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"],
                        exit_on_signal_loss=1)
    assert rc == 0, out
    log2 = _latest_decision_log(home)
    assert log2["status"] == "ok", out
    assert _order_count(fixture) == 1  # no re-buy in the resize run
    assert _live_order_count(fixture) == 1
    # The live stop attached at the LIVE filled qty only (1).
    live_stops = _live_stops(fixture)
    assert len(live_stops) == 1
    live_stop = next(iter(live_stops.values()))
    assert live_stop["qty"] == 1.0
    first_live_stop_id = live_stop["id"]
    live_coid = live_stop["client_order_id"]
    attached = _stop_sync_leg(log2, "stop_attached", "live")
    assert attached is not None
    assert attached["new_stop_qty"] == 1.0 and attached["held_qty"] == 1.0
    for step in attached["resize_trace"]:
        assert step["sellable_qty"] <= step["held_qty"]
    # The paper stop attached at 1 too (same partial fill).
    paper_attached = _stop_sync_leg(log2, "stop_attached", "paper")
    assert paper_attached is not None and paper_attached["new_stop_qty"] == 1.0
    # The 2nd share fills on both legs.
    paper_store = FileBackedFakeBroker(fixture)
    paper_store.fill(_open_order_coid(fixture, "NVDA"), "fill-2", 1, limit)
    live_store = _live_store(fixture)
    live_store.fill(live_orders[0]["client_order_id"], "fill-live-2", 1, limit)
    _reset_submitted_today(home)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"],
                        exit_on_signal_loss=1)
    assert rc == 0, out
    log3 = _latest_decision_log(home)
    assert log3["status"] == "ok", out
    # The live stop resized: old 1 -> new 2, cancel-old-then-attach-new.
    resized = _stop_sync_leg(log3, "stop_resized", "live")
    assert resized is not None
    assert resized["old_stop_qty"] == 1.0 and resized["new_stop_qty"] == 2.0
    assert resized["held_qty"] == 2.0
    trace = resized["resize_trace"]
    assert [s["sellable_qty"] for s in trace] == [1.0, 0.0, 2.0]
    assert [s["held_qty"] for s in trace] == [2.0, 2.0, 2.0]
    for step in trace:
        assert step["sellable_qty"] <= step["held_qty"]
    assert resized["unprotected_window"]["uncertain"] is False
    # A NEW broker id under the SAME deterministic coid.
    live_stops = _live_stops(fixture)
    assert len(live_stops) == 1
    live_stop = next(iter(live_stops.values()))
    assert live_stop["qty"] == 2.0
    assert live_stop["id"] != first_live_stop_id
    assert live_stop["client_order_id"] == live_coid
    # The paper stop resized to 2 as well.
    paper_resized = _stop_sync_leg(log3, "stop_resized", "paper")
    assert paper_resized is not None
    assert paper_resized["old_stop_qty"] == 1.0 and paper_resized["new_stop_qty"] == 2.0


# ---------------------------------------------------------------- B18.3 e2e

def test_live_oversell_detected_after_live_exit(tmp_path):
    """B18.3 e2e (live enabled): mirror the B17.4 paper test on the
    LIVE leg — the live stop elects between the live stop-cancel
    confirmation and the live exit submit. The live exit submits for
    2 while the live stop also sold 2, so the total committed (4)
    exceeds the live position held at exit time (2). The guardian
    halts and a CRITICAL alert fires. DETECTION, not prevention.
    The paper check does NOT fire (the paper stop did not elect)."""
    home = tmp_path / "home"
    fixture = tmp_path / "fixture"
    home.mkdir(parents=True)
    _, entry_price = _buy_fill_resize_live(home, fixture)
    live_stop_coid = _strategy_state(home)["open_entries"]["NVDA"]["live_trail_stop_coid"]
    assert live_stop_coid is not None
    live_store = _live_store(fixture)
    live_store.set_stop_elect_during_submit(live_stop_coid, 160.00)
    log2 = _run_exit(home, fixture, expect_status="needs_attention", live_enabled=True)
    assert _order_count(fixture) == 2  # paper buy + paper exit
    assert _live_order_count(fixture) == 2  # live buy + live exit
    detections = log2.get("oversell_detections") or []
    assert len(detections) == 1  # the paper check did not fire
    detection = detections[0]
    assert detection["account"] == "live"
    assert detection["symbol"] == "NVDA"
    assert detection["held_at_exit"] == 2.0
    assert detection["exit_qty"] == 2.0
    assert detection["stop_filled_qty"] == 2.0
    assert detection["total_committed"] == 4.0  # 2 (exit) + 2 (stop)
    assert "DETECTION" in detection["note"]
    assert "already happened" in detection["note"]
    assert "(live leg)" in detection["note"]
    alerts = _read_alerts(home)
    assert any(a.get("reason", "").startswith("OVERSELL DETECTED")
               and "(live leg)" in a.get("reason", "")
               for a in alerts)
    assert "CRITICAL" in _alerts_log(home)
    # The oversell is real, not a false alarm: filling the live exit
    # takes the live total sold to 4 against 2 held.
    live_exit_intent_id = _strategy_state(home)["pending_exits"]["live:NVDA"]["exit_intent_id"]
    live_store = _live_store(fixture)
    live_store.fill(live_exit_intent_id, "fill-live-exit-1", 2, 160.00)
    assert _live_total_sold(fixture, "NVDA") == 4.0
    assert _live_held(fixture, "NVDA") == -2.0  # the short is visible
    # The paper leg is unaffected: 2 held, 0 sold.
    assert _total_sold(fixture, "NVDA") == 0.0
    assert _held(fixture, "NVDA") == 2.0


# ---------------------------------------------------------------- B18.4 e2e

def test_pending_exit_filled_snapshot_shows_zero_pending(tmp_path):
    """B18.4 e2e: the exit submits unfilled, fills between runs, and
    the next run's pending_exit_filled snapshot shows exit_pending
    0.0 (not the stale 1.0 from the pre-fill record). The record
    itself is removed and unprotected is 0."""
    home = tmp_path / "home"
    fixture = tmp_path / "fixture"
    home.mkdir(parents=True)
    _, entry_price = _buy_fill_resize(home, fixture)
    log2 = _run_exit(home, fixture)
    assert _order_count(fixture) == 2  # buy + exit
    sell = next(
        i for i in _read_intents(home)
        if i["symbol"] == "NVDA" and i["side"] == "sell")
    assert sell["qty"] == 2.0 and sell["status"] == "SUBMITTED"
    store = FileBackedFakeBroker(fixture)
    store.fill(sell["intent_id"], "fill-exit-1", 2, 160.00)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"],
                        exit_on_signal_loss=1)
    assert rc == 0, out
    log3 = _latest_decision_log(home)
    assert log3["status"] == "ok", out
    snapshot = _protection(log3, "NVDA")
    assert snapshot is not None
    assert snapshot["action"] == "pending_exit_filled"
    assert snapshot["exit_pending"] == 0.0
    assert snapshot["held"] == 0.0
    assert snapshot["unprotected"] == 0.0
    assert snapshot["exposure_end_reason"] == "sold"
    assert _strategy_state(home)["pending_exits"] == {}
    # And the fill closed the decision at the real fill price (B18.5).
    decision = _decision(home, 1)
    assert decision["closed"] == 1
    assert decision["exit_price"] == 160.00
    assert decision["realized_pl"] == (160.00 - entry_price) * 2


# ---------------------------------------------------------------- B18.5 e2e + unit

def test_close_at_fill_not_submit(tmp_path):
    """B18.5 e2e: the exit submits unfilled — the decision stays
    OPEN (closed 0, submit-run summary trades_closed 0). The exit
    fills between runs at a price distinct from the quote — the next
    run closes the decision at the REAL fill price, and its summary
    reports trades_closed 1."""
    home = tmp_path / "home"
    fixture = tmp_path / "fixture"
    home.mkdir(parents=True)
    _, buy_coid, limit = _run_buy(home, fixture, exit_on_signal_loss=1)
    store = FileBackedFakeBroker(fixture)
    store.fill(buy_coid, "fill-1", 2, limit)
    entry_price = _strategy_state(home)["open_entries"]["NVDA"]["entry_price"]
    log2 = _run_exit(home, fixture)
    sell = next(
        i for i in _read_intents(home)
        if i["symbol"] == "NVDA" and i["side"] == "sell")
    assert sell["qty"] == 2.0 and sell["status"] == "SUBMITTED"
    assert _decision(home, 1)["closed"] == 0
    assert log2["summary"]["trades_closed"] == 0
    # The pending record tracks the fill; the stop sold nothing.
    pending = _strategy_state(home)["pending_exits"].get("paper:NVDA")
    assert pending is not None
    assert pending["stop_sold_qty"] == 0.0 and pending["stop_pl"] == 0.0
    quote_last = json.loads((fixture / "quotes.json").read_text())["NVDA"]["last"]
    exit_fill_price = round(quote_last + 5.0, 2)
    store = FileBackedFakeBroker(fixture)
    store.fill(sell["intent_id"], "fill-exit-1", 2, exit_fill_price)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"],
                        exit_on_signal_loss=1)
    assert rc == 0, out
    log3 = _latest_decision_log(home)
    assert log3["status"] == "ok", out
    decision = _decision(home, 1)
    assert decision["closed"] == 1
    assert decision["exit_price"] == exit_fill_price
    assert decision["realized_pl"] == (exit_fill_price - entry_price) * 2
    assert log3["summary"]["trades_closed"] == 1
    final = _stop_attribution(log3, "NVDA")
    assert final is not None and final.get("final_close") is True
    assert final["exit_price"] == exit_fill_price
    assert final["realized_pl"] == round(decision["realized_pl"], 2)


def _close_setup(tmp_path, *, entry_price: float = 167.25):
    """A TradeMemory with one open decision + tax lot, a guardian
    stub, and the pending record + FILLED intent for its exit.
    Returns (memory, guardian, pending, intent)."""
    memory = TradeMemory(str(tmp_path / "memory.db"))
    with sqlite3.connect(tmp_path / "memory.db") as conn:
        conn.execute(
            "INSERT INTO decisions (id, ts, symbol, direction, order_type,"
            " intended_price, qty) VALUES (1, '2026-11-27T00:00:00+00:00',"
            " 'NVDA', 'mild_up', 'limit', ?, ?)", (entry_price, 2.0))
        conn.commit()
    lot_id = memory.open_tax_lot("NVDA", 2.0, entry_price * 2.0)
    guardian = _guardian_stub()
    pending = {
        "symbol": "NVDA", "account": "paper", "exit_intent_id": "exit-1",
        "qty": 2.0, "filled_qty": 2.0, "entry_price": entry_price,
        "decision_id": 1, "lot_id": lot_id,
        "opened_at": "2026-11-27T00:00:00+00:00",
        "stop_sold_qty": 0.0, "stop_fill_price": None, "stop_pl": 0.0,
    }
    intent = {"intent_id": "exit-1", "status": "FILLED", "filled_qty": 2.0}
    return memory, guardian, pending, intent


def _stub_broker(lookup_result):
    """A broker whose leg lookup returns `lookup_result` (or raises
    it, if it is an exception instance)."""
    def lookup(coid):
        if isinstance(lookup_result, Exception):
            raise lookup_result
        return lookup_result
    leg = SimpleNamespace(get_by_client_order_id=lookup)
    return SimpleNamespace(paper=leg, live=leg)


def test_close_at_fill_with_real_price(tmp_path):
    """B18.5 (unit): the FILLED-branch close fetches the exit's
    filled_avg_price by order id and combines it with the stop
    attribution persisted at submit time."""
    memory, guardian, pending, intent = _close_setup(tmp_path)
    pending["stop_sold_qty"] = 1.0
    pending["stop_fill_price"] = 160.00
    pending["stop_pl"] = (160.00 - 167.25) * 1.0
    intent["filled_qty"] = 1.0  # the exit sold the other share
    broker = _stub_broker({"filled_avg_price": 165.00, "filled_qty": 1.0})
    result = _close_pending_exit_decision(broker, pending, intent, memory, guardian)
    assert result is not None and result["final_close"] is True
    assert result["exit_price"] == 165.00
    assert result["realized_pl"] == round(
        (160.00 - 167.25) * 1.0 + (165.00 - 167.25) * 1.0, 2)
    decision = [d for d in _decisions(tmp_path) if d["id"] == 1][0]
    assert decision["closed"] == 1
    assert decision["exit_price"] == 165.00
    assert decision["realized_pl"] == (160.00 - 167.25) * 1.0 + (165.00 - 167.25) * 1.0
    lot = [t for t in _tax_lots(tmp_path) if t["id"] == pending["lot_id"]][0]
    assert lot["proceeds"] == 160.00 * 1.0 + 165.00 * 1.0
    assert guardian.sent == []  # no alert on a clean close


def test_close_null_when_exit_price_unfetchable(tmp_path):
    """B18.5 (unit): when the exit's fill price cannot be fetched —
    lookup returns None, returns a dict with no filled_avg_price,
    or raises — the close records realized_pl NULL (unknown,
    explicitly not 0) + a CRITICAL alert (the B17.3 standard)."""
    for lookup_result in (None, {"filled_avg_price": None, "filled_qty": 2.0},
                          RuntimeError("broker down")):
        # A fresh memory db per case (each setup inserts decision 1).
        case = tmp_path / f"case{len(list(tmp_path.iterdir()))}"
        case.mkdir()
        memory, guardian, pending, intent = _close_setup(case)
        broker = _stub_broker(lookup_result)
        result = _close_pending_exit_decision(broker, pending, intent, memory, guardian)
        assert result is not None and result["final_close"] is True
        assert result["exit_price"] is None
        assert result["realized_pl"] is None
        decision = [d for d in _decisions(case) if d["id"] == 1][0]
        assert decision["closed"] == 1
        assert decision["exit_price"] is None
        assert decision["realized_pl"] is None
        assert any(level == "critical" and "UNKNOWN" in msg
                   for level, msg in guardian.sent)
        # The tax lot closes with NULL proceeds (the price is unknown).
        lot = [t for t in _tax_lots(case) if t["id"] == pending["lot_id"]][0]
        assert lot["proceeds"] is None


def test_close_skips_already_closed_decision(tmp_path):
    """B18.5 (unit): the double-close guard — a decision already
    closed (a stale pre-B18.5 pending record, whose decision was
    closed at submit time) is NOT closed again; its recorded P&L
    is preserved, and nothing is appended."""
    memory, guardian, pending, intent = _close_setup(tmp_path)
    memory.close_decision(1, 160.00, -14.50, 0.5)
    broker = _stub_broker({"filled_avg_price": 165.00, "filled_qty": 2.0})
    result = _close_pending_exit_decision(broker, pending, intent, memory, guardian)
    assert result is None
    decision = [d for d in _decisions(tmp_path) if d["id"] == 1][0]
    assert decision["exit_price"] == 160.00
    assert decision["realized_pl"] == -14.50


def test_cancelled_exit_keeps_decision_open_no_double_close(tmp_path):
    """B18.5 e2e: the exit submits, is CANCELLED before filling —
    the decision stays OPEN, the entry is re-created, and the
    re-derived exit (fresh intent id) closes the decision at ITS
    fill. No double-close: the first (cancelled) exit never closed
    it, and the recorded exit price is the second exit's fill."""
    home = tmp_path / "home"
    fixture = tmp_path / "fixture"
    home.mkdir(parents=True)
    _, buy_coid, limit = _run_buy(home, fixture, exit_on_signal_loss=1)
    store = FileBackedFakeBroker(fixture)
    store.fill(buy_coid, "fill-1", 2, limit)
    entry_price = _strategy_state(home)["open_entries"]["NVDA"]["entry_price"]
    log2 = _run_exit(home, fixture)
    exit1 = next(
        i for i in _read_intents(home)
        if i["symbol"] == "NVDA" and i["side"] == "sell")
    assert exit1["qty"] == 2.0 and exit1["status"] == "SUBMITTED"
    assert _decision(home, 1)["closed"] == 0
    # Cancel the exit before it fills.
    store = FileBackedFakeBroker(fixture)
    store.cancel(exit1["intent_id"])
    # Run 3: the startup sync sees CANCELLED (entry re-created) and
    # the exit loop re-derives the exit with a FRESH intent id.
    log3 = _run_exit(home, fixture)
    exit1_after = next(
        i for i in _read_intents(home)
        if i["intent_id"] == exit1["intent_id"])
    assert exit1_after["status"] == "CANCELLED"
    exits = [i for i in _read_intents(home)
             if i["symbol"] == "NVDA" and i["side"] == "sell"]
    assert len(exits) == 2
    exit2 = exits[1]
    assert exit2["intent_id"] != exit1["intent_id"]
    assert exit2["qty"] == 2.0 and exit2["status"] == "SUBMITTED"
    # The entry attempt bumped again (the re-derived exit's submit
    # ended the re-created trade), but the DECISION is still open.
    assert _strategy_state(home)["entry_attempts"] == {"NVDA": 2}
    assert _decision(home, 1)["closed"] == 0
    # The cancelled exit fired its alert (to alerts.log — the
    # CANCELLED branch alerts via the alerter, not the intent store).
    assert "CANCELLED" in _alerts_log(home)
    # Fill the SECOND exit at a distinctive price; Run 4 closes the
    # decision at THAT price — the first exit's (never-filled) price
    # is nowhere in the record.
    quote_last = json.loads((fixture / "quotes.json").read_text())["NVDA"]["last"]
    exit2_fill_price = round(quote_last + 5.0, 2)
    store = FileBackedFakeBroker(fixture)
    store.fill(exit2["intent_id"], "fill-exit-2", 2, exit2_fill_price)
    # Three submit runs compressed into <60s record 6 guardian.submit
    # invocations (paper + the disabled-live-leg no-op each) — clear
    # the 60s rate window so Run 4 starts like a real (next-day) run.
    _reset_order_rate(home)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"],
                        exit_on_signal_loss=1)
    assert rc == 0, out
    log4 = _latest_decision_log(home)
    assert log4["status"] == "ok", out
    decision = _decision(home, 1)
    assert decision["closed"] == 1
    assert decision["exit_price"] == exit2_fill_price
    assert decision["realized_pl"] == (exit2_fill_price - entry_price) * 2
    assert log4["summary"]["trades_closed"] == 1
    exit2_after = next(
        i for i in _read_intents(home)
        if i["intent_id"] == exit2["intent_id"])
    assert exit2_after["status"] == "FILLED"
