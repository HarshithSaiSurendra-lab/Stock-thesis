"""B17 — the remaining-exposure fixes for soak readiness.

Covers B17.2 (startup mirror of trailing-stop fills), B17.3
(attribute stop-fill P&L), B17.4 (race detection), B17.5 (live-leg
exact-once protection) and B17.6 (real-leg lookup returns prices),
plus the three-outcome stop-coordination trace (follow-up 1), the
unfilled-exit exposure trace (follow-up 2 / Detail 2) and the
oversized-stop interval (follow-up 2 / Detail 3, follow-up 3).

OCO/bracket orders are DEFERRED (user decision, Nov 27, 2026):
nothing here changes entry order types. B17.4 is DETECTION + halt,
not prevention — the oversell has already happened by the time it
fires; the halt and alert stay regardless.

All tests use fake clients/fixtures only; the e2e runs main.py as a
subprocess, same harness as B13/B15/B16. No real or paper orders.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

from fake_broker import FakeBroker  # noqa: E402
from fake_dual_broker import FakeDualBroker, FileBackedFakeBroker  # noqa: E402
from memory import TradeMemory  # noqa: E402
from strategy_runner import (  # noqa: E402
    StrategyState, _mirror_stop_fills_at_startup,
    _reservation_price_for_order, _stop_fill_state,
)
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
    _alerts_log, _held, _stop_coid_for, _stops, _total_sold,
)

VENV_PY = REPO / ".venv" / "bin" / "python"


# ---------------------------------------------------------------- helpers

def _fake_env(home: Path, fixture: Path) -> dict:
    env = _env(home, fixture)
    env["ORDER_STORE_PATH"] = str(home / "order_intents.db")
    env["ATS_FAKE_BROKER_DIR"] = str(fixture)
    return env


def _decisions(home: Path) -> list[dict]:
    db = home / "memory.db"
    if not db.exists():
        return []
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT id, symbol, closed, exit_price, realized_pl, holding_days,"
            " actually_up FROM decisions ORDER BY id")]


def _decision(home: Path, decision_id: int) -> dict:
    rows = [d for d in _decisions(home) if d["id"] == decision_id]
    assert len(rows) == 1, decision_id
    return rows[0]


def _tax_lots(home: Path) -> list[dict]:
    db = home / "memory.db"
    if not db.exists():
        return []
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT id, symbol, qty, cost_basis, proceeds, realized_gain, close_ts"
            " FROM tax_lots ORDER BY id")]


def _protection(log: dict, symbol: str) -> dict | None:
    for p in log.get("protection") or []:
        if p.get("symbol") == symbol:
            return p
    return None


def _stop_sync(log: dict, action: str) -> dict | None:
    for e in log.get("stop_sync") or []:
        if e.get("action") == action:
            return e
    return None


def _stop_attribution(log: dict, symbol: str) -> dict | None:
    for a in log.get("stop_attribution") or []:
        if a.get("symbol") == symbol:
            return a
    return None


def _buy_fill_resize(home: Path, fixture: Path) -> tuple[float, float]:
    """Run 1 buys NVDA qty 2 (unfilled), the buy fills 2, and the
    next run's startup size sync attaches a stop sized to the filled
    qty (2). Returns (limit, entry_price)."""
    _, coid, limit = _run_buy(home, fixture, exit_on_signal_loss=1)
    store = FileBackedFakeBroker(fixture)
    store.fill(coid, "fill-1", 2, limit)
    # The reset is essential here: symbols in submitted_today are
    # deduped out of the candidate list, and the exit loop reads "not
    # a candidate" as "signal lost" — without the reset this run
    # would exit the position instead of attaching the stop.
    _reset_submitted_today(home)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"],
                        exit_on_signal_loss=1)
    assert rc == 0, out
    log = _latest_decision_log(home)
    assert log["status"] == "ok", out
    stop_coid = _stop_coid_for(fixture, "NVDA")
    assert _stops(fixture)[stop_coid]["qty"] == 2.0
    entry_price = _strategy_state(home)["open_entries"]["NVDA"]["entry_price"]
    # Restore submitted_today (Run 1.5's reset is persisted by its
    # save): this is still the same trading day, and NVDA was
    # submitted — the next run must not re-buy it if the mirror
    # removes the entry (same-day re-entry; see the full-fill test).
    state_path = home / "strategy_state.json"
    state = json.loads(state_path.read_text())
    if "NVDA" not in state.get("submitted_today", []):
        state["submitted_today"] = state.get("submitted_today", []) + ["NVDA"]
    state_path.write_text(json.dumps(state))
    return limit, entry_price


def _run_exit(home: Path, fixture: Path, *, expect_status: str = "ok",
              live_enabled: bool = False) -> dict:
    """An exit run (NVDA falling): the exit loop runs. Returns the
    decision log. expect_status is "ok" normally; a run that halts
    (e.g. B17.4's oversell detection) expects "needs_attention".
    live_enabled re-applies the live-leg config after _write_fixture
    rewrites broker_config.json (which would otherwise disable it)."""
    _write_fixture(fixture, nvda_cycle=NVDA_FALLING)
    if live_enabled:
        _set_live_enabled(fixture)
    _reset_submitted_today(home)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"],
                        exit_on_signal_loss=1)
    assert rc == 0, out
    log = _latest_decision_log(home)
    assert log["status"] == expect_status, out
    return log


def _fill_buy(store: FileBackedFakeBroker, qty: float, price: float) -> None:
    """Fill the (entry) buy through the store so positions() shows a
    real holding — the trailing-stop trigger caps its fill at held,
    so a mirror test with no holding would (correctly) sell 0."""
    store.submit({
        "symbol": "NVDA", "qty": qty, "side": "buy", "type": "limit",
        "limit_price": price, "time_in_force": "day",
        "client_order_id": "buy-1"})
    store.fill("buy-1", "fill-1", qty, price)


def _guardian_stub() -> SimpleNamespace:
    sent: list[tuple[str, str]] = []

    def send(level: str, msg: str) -> None:
        sent.append((level, msg))

    guardian = SimpleNamespace(alerter=SimpleNamespace(send=send))
    guardian.sent = sent
    return guardian


def _live_store(fixture: Path) -> FileBackedFakeBroker:
    return FileBackedFakeBroker(fixture, state_filename="broker_state_live.json")


def _live_order_count(fixture: Path) -> int:
    return _live_store(fixture).order_count()


def _set_live_enabled(fixture: Path) -> None:
    """Patch broker_config.json (after _write_fixture, which rewrites
    it) so the fake live leg is enabled for the next run."""
    path = fixture / "broker_config.json"
    config = json.loads(path.read_text())
    config["live_enabled"] = True
    path.write_text(json.dumps(config))


# ---------------------------------------------------------------- B17.2 unit

def test_stop_fill_state_working_stop(tmp_path):
    fixture = tmp_path / "fixture"
    _write_fixture(fixture)
    broker = FakeDualBroker(fixture)
    stop = broker.paper.attach_trailing_stop("NVDA", 2.0, 0.06)
    state = _stop_fill_state(
        broker.paper, symbol="NVDA", trail_stop_id=stop["id"],
        trail_stop_coid=stop["client_order_id"],
        lookup_fn=broker.paper.get_by_client_order_id)
    assert state["found"] is True and state["ambiguous"] is False
    assert state["status"] == "submitted" and state["filled_qty"] == 0.0


def test_stop_fill_state_cancelled_by_order_id(tmp_path):
    """The stop's final state is read BY ORDER ID — even when the
    stop is invisible to open_orders (Detail 1)."""
    fixture = tmp_path / "fixture"
    _write_fixture(fixture)
    broker = FakeDualBroker(fixture)
    stop = broker.paper.attach_trailing_stop("NVDA", 2.0, 0.06)
    # cancel_order's first positional is order_id, NOT the client
    # order id — pass the keyword (passing the coid positionally
    # silently no-ops: it looks for an order whose id equals the
    # coid).
    broker.paper.cancel_order(client_order_id=stop["client_order_id"])
    assert broker.paper.open_orders() == []
    state = _stop_fill_state(
        broker.paper, symbol="NVDA", trail_stop_id=stop["id"],
        trail_stop_coid=stop["client_order_id"],
        lookup_fn=broker.paper.get_by_client_order_id)
    assert state["found"] is True and state["ambiguous"] is False
    assert state["status"] == "cancelled"


def test_stop_fill_state_ambiguous_when_lookup_inconclusive(tmp_path):
    """A recorded stop that is absent from open_orders AND whose
    lookup is inconclusive is AMBIGUOUS — never infer its final state
    from its absence (Detail 1)."""
    fixture = tmp_path / "fixture"
    _write_fixture(fixture)
    broker = FakeDualBroker(fixture)
    stop = broker.paper.attach_trailing_stop("NVDA", 2.0, 0.06)
    # Hide the stop from the open-orders view (as a dropped cancel
    # response would) and make the lookup fail.
    # cancel_order's first positional is order_id, NOT the client
    # order id — pass the keyword (passing the coid positionally
    # silently no-ops: it looks for an order whose id equals the
    # coid).
    broker.paper.cancel_order(client_order_id=stop["client_order_id"])
    state = _stop_fill_state(
        broker.paper, symbol="NVDA", trail_stop_id=stop["id"],
        trail_stop_coid=stop["client_order_id"],
        lookup_fn=lambda _coid: (_ for _ in ()).throw(RuntimeError("lookup down")))
    assert state["ambiguous"] is True


def test_stop_fill_state_no_stop_recorded(tmp_path):
    fixture = tmp_path / "fixture"
    _write_fixture(fixture)
    broker = FakeDualBroker(fixture)
    state = _stop_fill_state(
        broker.paper, symbol="NVDA", trail_stop_id=None,
        trail_stop_coid=None, lookup_fn=broker.paper.get_by_client_order_id)
    assert state["found"] is False and state["ambiguous"] is False


def _mirror_state(tmp_path, *, qty: float, entry_price: float = 167.25) -> tuple:
    """A StrategyState with one NVDA entry + a TradeMemory holding
    its decision and tax lot. Returns (store, broker, guardian,
    state, memory, decision_id, lot_id): `store` is the
    FileBackedFakeBroker used for setup (attach / trigger / fill);
    `broker` is a FRESH FakeDualBroker whose leg loads that setup
    from the state file, so the mirror sees the broker's true state
    (no stale in-memory copy)."""
    fixture = tmp_path / "fixture"
    _write_fixture(fixture)
    store = FileBackedFakeBroker(fixture)
    broker = FakeDualBroker(fixture)
    guardian = _guardian_stub()
    state = StrategyState()
    state.open_entries["NVDA"] = {
        "symbol": "NVDA", "qty": qty, "side": "buy", "direction": "mild_up",
        "decision_id": 1, "lot_id": 1, "entry_price": entry_price,
        "opened_at": "2026-11-27T00:00:00+00:00",
        "trail_percent": 0.06, "trail_stop_id": None, "trail_stop_coid": None,
    }
    memory = TradeMemory(str(tmp_path / "memory.db"))
    with sqlite3.connect(tmp_path / "memory.db") as conn:
        conn.execute(
            "INSERT INTO decisions (id, ts, symbol, direction, order_type,"
            " intended_price, qty) VALUES (1, '2026-11-27T00:00:00+00:00',"
            " 'NVDA', 'mild_up', 'limit', ?, ?)", (entry_price, qty))
        conn.commit()
    lot_id = memory.open_tax_lot("NVDA", qty, entry_price * qty)
    state.open_entries["NVDA"]["lot_id"] = lot_id
    return store, broker, guardian, state, memory, 1, lot_id


def test_mirror_removes_entry_and_closes_decision(tmp_path):
    """B17.2 + B17.3 (unit): the stop sold everything overnight — the
    entry is removed, the decision is closed with the stop's fill
    price and a non-zero realized_pl, and the tax lot is closed."""
    store, broker, guardian, state, memory, decision_id, lot_id = _mirror_state(tmp_path, qty=2.0)
    _fill_buy(store, 2.0, 167.25)
    stop = store.attach_trailing_stop("NVDA", 2.0, 0.06)
    store.trigger_trailing_stop(stop["client_order_id"], 160.00)
    state.open_entries["NVDA"]["trail_stop_id"] = stop["id"]
    state.open_entries["NVDA"]["trail_stop_coid"] = stop["client_order_id"]
    broker = FakeDualBroker(tmp_path / "fixture")  # fresh leg: loads the trigger
    synced = _mirror_stop_fills_at_startup(broker, guardian, state, memory=memory)
    assert state.open_entries == {}
    entry = _stop_sync({"stop_sync": synced}, "stop_fill_mirrored_removed")
    assert entry is not None
    assert entry["stop_filled_qty"] == 2.0
    assert entry["stop_filled_avg_price"] == 160.00
    assert entry["decision_closed"] is True
    assert entry["realized_pl"] == round((160.00 - 167.25) * 2, 2)  # -14.50
    decision = [d for d in _decisions(tmp_path) if d["id"] == decision_id][0]
    assert decision["closed"] == 1
    assert decision["exit_price"] == 160.00
    assert decision["realized_pl"] == (160.00 - 167.25) * 2
    lot = [t for t in _tax_lots(tmp_path) if t["id"] == lot_id][0]
    assert lot["close_ts"] is not None and lot["proceeds"] == 160.00 * 2


def test_mirror_reduces_entry_and_keeps_decision_open(tmp_path):
    store, broker, guardian, state, memory, decision_id, lot_id = _mirror_state(tmp_path, qty=2.0)
    _fill_buy(store, 2.0, 167.25)
    stop = store.attach_trailing_stop("NVDA", 2.0, 0.06)
    store.fill(stop["client_order_id"], "stop-fill-1", 1, 160.00)
    state.open_entries["NVDA"]["trail_stop_id"] = stop["id"]
    state.open_entries["NVDA"]["trail_stop_coid"] = stop["client_order_id"]
    broker = FakeDualBroker(tmp_path / "fixture")  # fresh leg: loads the fill
    synced = _mirror_stop_fills_at_startup(broker, guardian, state, memory=memory)
    assert state.open_entries["NVDA"]["qty"] == 1.0
    entry = _stop_sync({"stop_sync": synced}, "stop_fill_mirrored_reduced")
    assert entry is not None and entry["stop_filled_qty"] == 1.0
    assert entry["decision_closed"] is False
    decision = [d for d in _decisions(tmp_path) if d["id"] == decision_id][0]
    assert decision["closed"] == 0  # the trade is still open


def test_mirror_unaccounted_shortfall_does_not_mirror(tmp_path):
    store, broker, guardian, state, memory, decision_id, lot_id = _mirror_state(tmp_path, qty=2.0)
    stop = store.attach_trailing_stop("NVDA", 2.0, 0.06)
    # The position is 1 (the buy filled 1 of 2) but the stop filled
    # 0 — the shortfall is NOT the stop's doing: genuine drift.
    state.open_entries["NVDA"]["trail_stop_id"] = stop["id"]
    state.open_entries["NVDA"]["trail_stop_coid"] = stop["client_order_id"]
    _fill_buy(store, 1.0, 167.25)
    broker = FakeDualBroker(tmp_path / "fixture")  # fresh leg: loads the fill
    synced = _mirror_stop_fills_at_startup(broker, guardian, state, memory=memory)
    assert state.open_entries["NVDA"]["qty"] == 2.0  # untouched
    assert _stop_sync({"stop_sync": synced}, "unaccounted_shortfall") is not None


def test_mirror_ambiguous_stop_blocks_exit(tmp_path):
    store, broker, guardian, state, memory, decision_id, lot_id = _mirror_state(tmp_path, qty=2.0)
    _fill_buy(store, 2.0, 167.25)
    stop = store.attach_trailing_stop("NVDA", 2.0, 0.06)
    state.open_entries["NVDA"]["trail_stop_id"] = stop["id"]
    state.open_entries["NVDA"]["trail_stop_coid"] = stop["client_order_id"]
    # Force ambiguity: hide the stop from the open-orders view (the
    # cancel is applied) AND make the lookup raise, so the mirror
    # cannot confirm the stop's final state by order id. It must NOT
    # infer the state from the stop's absence, and must mark the entry
    # so the exit stays BLOCKED across restart.
    broker = FakeDualBroker(tmp_path / "fixture")  # fresh leg: loads the stop
    broker.paper.get_by_client_order_id = lambda _coid: (_ for _ in ()).throw(
        RuntimeError("lookup down"))
    # cancel_order's first positional is order_id, NOT the client
    # order id — pass the keyword (passing the coid positionally
    # silently no-ops: it looks for an order whose id equals the
    # coid).
    broker.paper.cancel_order(client_order_id=stop["client_order_id"])
    synced = _mirror_stop_fills_at_startup(broker, guardian, state, memory=memory)
    assert state.open_entries["NVDA"].get("stop_state_unknown") is True
    assert _stop_sync({"stop_sync": synced}, "stop_state_unknown") is not None


def test_mirror_unknown_price_records_null_pl_and_alerts(tmp_path):
    """B17.3 (unit): the stop sold everything but its fill price
    cannot be fetched — realized_pl is NULL (unknown), explicitly
    not 0, and a CRITICAL alert fires."""
    store, broker, guardian, state, memory, decision_id, lot_id = _mirror_state(tmp_path, qty=2.0)
    _fill_buy(store, 2.0, 167.25)
    stop = store.attach_trailing_stop("NVDA", 2.0, 0.06)
    store.trigger_trailing_stop(stop["client_order_id"], 160.00)
    state.open_entries["NVDA"]["trail_stop_id"] = stop["id"]
    state.open_entries["NVDA"]["trail_stop_coid"] = stop["client_order_id"]
    # The lookup returns no filled_avg_price (the price is unknown).
    broker = FakeDualBroker(tmp_path / "fixture")  # fresh leg: loads the trigger
    original_lookup = broker.paper.get_by_client_order_id
    def no_price(coid):
        found = original_lookup(coid)
        if found is not None:
            found = dict(found)
            found["filled_avg_price"] = None
        return found
    broker.paper.get_by_client_order_id = no_price
    synced = _mirror_stop_fills_at_startup(broker, guardian, state, memory=memory)
    assert state.open_entries == {}
    entry = _stop_sync({"stop_sync": synced}, "stop_fill_mirrored_removed")
    assert entry is not None and entry["realized_pl"] is None
    decision = [d for d in _decisions(tmp_path) if d["id"] == decision_id][0]
    assert decision["closed"] == 1
    assert decision["realized_pl"] is None  # UNKNOWN, explicitly not 0
    assert any(level == "critical" and "UNKNOWN" in msg
               for level, msg in guardian.sent)


# ---------------------------------------------------------------- B17.2 e2e

def test_b172_stop_fires_overnight_full(tmp_path):
    """B17.2 e2e: the stop fires overnight with no exit — the next
    run mirrors the entry (removes it), no drift halt, stop_sync
    records it; B17.3: the decision records the stop's fill price
    and a non-zero realized_pl."""
    home = tmp_path / "home"
    fixture = tmp_path / "fixture"
    home.mkdir(parents=True)
    limit, entry_price = _buy_fill_resize(home, fixture)
    stop_coid = _stop_coid_for(fixture, "NVDA")
    store = FileBackedFakeBroker(fixture)
    triggered = store.trigger_trailing_stop(stop_coid, 160.00)
    assert triggered["qty"] == 2.0 and triggered["price"] == 160.00
    assert _stops(fixture)[stop_coid]["filled_qty"] == 2.0
    assert _stops(fixture)[stop_coid]["filled_avg_price"] == 160.00
    assert _held(fixture, "NVDA") == 0.0
    # The signal is still up: a plain run, no exit. Do NOT reset
    # submitted_today — this is the same trading day, so the entry
    # loop must not re-buy NVDA (see the same-day re-entry note
    # above).
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"],
                        exit_on_signal_loss=1)
    assert rc == 0, out
    log = _latest_decision_log(home)
    assert log["status"] == "ok", out  # no drift halt
    assert _strategy_state(home)["open_entries"] == {}
    entry = _stop_sync(log, "stop_fill_mirrored_removed")
    assert entry is not None
    assert entry["stop_filled_qty"] == 2.0
    assert entry["stop_filled_avg_price"] == 160.00
    assert entry["decision_closed"] is True
    decision = _decision(home, 1)
    assert decision["closed"] == 1
    assert decision["exit_price"] == 160.00
    assert decision["realized_pl"] == (160.00 - entry_price) * 2  # -14.50ish
    assert decision["realized_pl"] != 0.0


def test_b172_stop_fires_partially(tmp_path):
    """B17.2 e2e: the stop fires partially overnight — the next run
    reduces the entry to the held qty (1), no halt."""
    home = tmp_path / "home"
    fixture = tmp_path / "fixture"
    home.mkdir(parents=True)
    limit, entry_price = _buy_fill_resize(home, fixture)
    stop_coid = _stop_coid_for(fixture, "NVDA")
    store = FileBackedFakeBroker(fixture)
    store.fill(stop_coid, "stop-fill-1", 1, 160.00)
    assert _held(fixture, "NVDA") == 1.0
    _reset_submitted_today(home)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"],
                        exit_on_signal_loss=1)
    assert rc == 0, out
    log = _latest_decision_log(home)
    assert log["status"] == "ok", out
    assert _strategy_state(home)["open_entries"]["NVDA"]["qty"] == 1.0
    entry = _stop_sync(log, "stop_fill_mirrored_reduced")
    assert entry is not None and entry["stop_filled_qty"] == 1.0
    assert entry["decision_closed"] is False
    assert _decision(home, 1)["closed"] == 0  # the trade is still open


# ---------------------------------------------------------------- B17.3 + trace (a)

def test_b173_stop_fills_during_cancel(tmp_path):
    """B17.3 e2e + three-outcome trace (a): the stop FILLS while
    cancellation is pending — the exit asked to cancel the stop and
    the stop elected and filled before the cancel confirmed. The
    exit stays BLOCKED while the stop can still execute (no exit is
    submitted while its final state is unconfirmed); once confirmed,
    the exit is sized from the REMAINING position (0 — never the
    pre-cancel 2); the decision records the stop's fill price and a
    non-zero realized_pl; total sold never exceeds held."""
    home = tmp_path / "home"
    fixture = tmp_path / "fixture"
    home.mkdir(parents=True)
    limit, entry_price = _buy_fill_resize(home, fixture)
    stop_coid = _stop_coid_for(fixture, "NVDA")
    store = FileBackedFakeBroker(fixture)
    store.set_fill_during_stop_cancel(stop_coid, 2, 160.00)
    log2 = _run_exit(home, fixture)
    assert _order_count(fixture) == 1  # buy only: NO sell submitted
    assert _total_sold(fixture, "NVDA") == 2.0  # all from the stop
    assert _held(fixture, "NVDA") == 0.0
    assert _strategy_state(home)["open_entries"] == {}
    attribution = _stop_attribution(log2, "NVDA")
    assert attribution is not None
    assert attribution["stop_sold_qty"] == 2.0
    assert attribution["stop_filled_avg_price"] == 160.00
    assert attribution["exit_qty"] == 0.0  # sized from the REMAINING position
    decision = _decision(home, 1)
    assert decision["closed"] == 1
    assert decision["exit_price"] == 160.00
    assert decision["realized_pl"] == (160.00 - entry_price) * 2
    assert decision["realized_pl"] != 0.0
    # The shares were sold by the stop, never left unprotected: no
    # unprotected alert for this symbol.
    assert not any("unprotected" in a.get("reason", "")
                   for a in _read_alerts(home))


# ---------------------------------------------------------------- B17.4

def test_b174_race_detection(tmp_path):
    """B17.4 e2e: the stop elects between the cancel confirmation
    and the exit submit — the exit submits for 2 while the stop also
    sold 2, so the total committed (4) exceeds the position held at
    exit time (2). The guardian halts and a CRITICAL alert fires.
    This is DETECTION + halt, NOT prevention: the oversell has
    already happened (OCO/bracket is deferred)."""
    home = tmp_path / "home"
    fixture = tmp_path / "fixture"
    home.mkdir(parents=True)
    limit, entry_price = _buy_fill_resize(home, fixture)
    stop_coid = _stop_coid_for(fixture, "NVDA")
    store = FileBackedFakeBroker(fixture)
    store.set_stop_elect_during_submit(stop_coid, 160.00)
    # The oversell detection halts the guardian: this run's status is
    # needs_attention, not ok.
    log2 = _run_exit(home, fixture, expect_status="needs_attention")
    assert _order_count(fixture) == 2  # buy + exit
    detections = log2.get("oversell_detections") or []
    assert len(detections) == 1
    detection = detections[0]
    assert detection["symbol"] == "NVDA"
    assert detection["held_at_exit"] == 2.0
    assert detection["exit_qty"] == 2.0
    assert detection["stop_filled_qty"] == 2.0
    assert detection["total_committed"] == 4.0  # 2 (exit) + 2 (stop)
    assert "DETECTION" in detection["note"]
    assert "already happened" in detection["note"]
    alerts = _read_alerts(home)
    assert any(a.get("reason", "").startswith("OVERSELL DETECTED")
               for a in alerts)
    assert "CRITICAL" in _alerts_log(home)
    # The oversell is real, not a false alarm: filling the exit sell
    # takes the total sold to 4 against 2 held.
    exit_intent = next(
        i for i in _read_intents(home)
        if i["symbol"] == "NVDA" and i["side"] == "sell")
    store = FileBackedFakeBroker(fixture)
    store.fill(exit_intent["intent_id"], "fill-exit-1", 2, 160.00)
    assert _total_sold(fixture, "NVDA") == 4.0
    assert _held(fixture, "NVDA") == -2.0  # the short is visible

# ---------------------------------------------------------------- three-outcome trace (b) and (c)

def test_trace_stop_partially_fills_during_cancel(tmp_path):
    """Three-outcome trace (b): the stop PARTIALLY FILLS before
    cancellation is confirmed — the stop sells 1 share, then the
    cancel confirms for the remainder. The exit stays BLOCKED while
    the stop can still execute; once confirmed, the exit is sized
    from the REMAINING position (1 — never the pre-cancel 2); the
    remaining share is unprotected from the stop-cancel confirmation
    to the exit submission (recorded in the pending-exit record with
    exposure_uncertain False); total sold never exceeds held."""
    home = tmp_path / "home"
    fixture = tmp_path / "fixture"
    home.mkdir(parents=True)
    limit, entry_price = _buy_fill_resize(home, fixture)
    stop_coid = _stop_coid_for(fixture, "NVDA")
    store = FileBackedFakeBroker(fixture)
    store.set_fill_during_stop_cancel(stop_coid, 1, 160.00)
    log2 = _run_exit(home, fixture)
    assert _order_count(fixture) == 2  # buy + the re-sized exit
    sell = next(
        i for i in _read_intents(home)
        if i["symbol"] == "NVDA" and i["side"] == "sell")
    assert sell["qty"] == 1.0  # sized from the REMAINING position
    assert sell["status"] == "SUBMITTED"
    assert _total_sold(fixture, "NVDA") == 1.0  # the stop's 1; the sell is unfilled
    assert _held(fixture, "NVDA") == 1.0
    attribution = _stop_attribution(log2, "NVDA")
    assert attribution is not None
    assert attribution["stop_sold_qty"] == 1.0
    assert attribution["stop_filled_avg_price"] == 160.00
    assert attribution["exit_qty"] == 1.0
    # The exit is accepted but unfilled: the pending record tracks
    # the remaining share's exposure (unprotected from the
    # stop-cancel confirmation to the exit submission/confirmation).
    pending = _strategy_state(home)["pending_exits"].get("paper:NVDA")
    assert pending is not None
    assert pending["qty"] == 1.0 and pending["filled_qty"] == 0.0
    assert pending["exposure_uncertain"] is False
    assert pending["exposure_started_at"] is not None
    # Filling the exit sell takes the total sold to exactly 2 (never
    # more than the 2 held at exit time).
    store = FileBackedFakeBroker(fixture)
    store.fill(sell["intent_id"], "fill-exit-1", 1, 160.00)
    assert _total_sold(fixture, "NVDA") == 2.0
    assert _held(fixture, "NVDA") == 0.0
    decision = _decision(home, 1)
    assert decision["closed"] == 1
    # Combined P&L: the stop's 1 share at 160.00 + the exit's 1 share
    # at the decision's exit price, both from the entry price.
    assert decision["realized_pl"] == (
        (160.00 - entry_price) * 1 + (decision["exit_price"] - entry_price) * 1)


def test_trace_cancel_unknown_across_restart(tmp_path):
    """Three-outcome trace (c): cancellation status stays UNKNOWN
    across restart — the cancel response is dropped / the lookup is
    inconclusive, and the run ends; the next run's startup reconcile
    looks the stop up BY ORDER ID. The exit stays BLOCKED while the
    stop can still execute (no exit submitted in Run 2); once the
    stop's final state is confirmed (cancelled), the exit is sized
    from the REMAINING position (2); the unprotected window is
    recorded with its uncertainty EXPLICIT (the true duration is
    bounded by the cancel-request time and the confirmation time,
    because the exact moment the stop stopped protecting is
    unknowable while its status is unknown)."""
    home = tmp_path / "home"
    fixture = tmp_path / "fixture"
    home.mkdir(parents=True)
    limit, entry_price = _buy_fill_resize(home, fixture)
    stop_coid = _stop_coid_for(fixture, "NVDA")
    store = FileBackedFakeBroker(fixture)
    store.set_drop_next_stop_cancel()
    log2 = _run_exit(home, fixture)
    # Run 2: the exit is BLOCKED (skipped), no sell submitted, the
    # entry stays open, and the cancel-request time is recorded on
    # the entry for the next run.
    assert _order_count(fixture) == 1  # buy only
    assert _total_sold(fixture, "NVDA") == 0.0
    assert _strategy_state(home)["open_entries"]["NVDA"]["qty"] == 2.0
    assert any(s.get("stage") == "exit_stop_cancel" for s in log2.get("skipped") or [])
    requested_at = _strategy_state(home)["open_entries"]["NVDA"].get(
        "stop_cancel_requested_at")
    assert requested_at is not None
    # Run 3 (restart): the startup mirror looks the stop up BY ORDER
    # ID, finds it cancelled, records the confirmation with the
    # bounded, uncertain unprotected window — and the exit proceeds.
    log3 = _run_exit(home, fixture)
    confirmed = _stop_sync(log3, "stop_cancel_confirmed")
    assert confirmed is not None
    assert confirmed["stop_status"] == "cancelled"
    window = confirmed["unprotected_window"]
    assert window["uncertain"] is True
    assert window["start"] == requested_at
    assert window["end"] > window["start"]
    assert "unknowable" in window["note"]
    assert _order_count(fixture) == 2  # buy + the exit (sized from the REMAINING 2)
    sell = next(
        i for i in _read_intents(home)
        if i["symbol"] == "NVDA" and i["side"] == "sell")
    assert sell["qty"] == 2.0 and sell["status"] == "SUBMITTED"
    assert _held(fixture, "NVDA") == 2.0  # the exit is unfilled; nothing sold yet


# ---------------------------------------------------------------- unfilled exit (Detail 2)

def test_unfilled_exit_across_restart(tmp_path):
    """Detail 2 + follow-up 3: the stop is CONFIRMED cancelled and
    the replacement exit is ACCEPTED but remains UNFILLED across
    restart. Held, exit-pending, and protected are shown SEPARATELY,
    and the exposure record is DURABLE (persisted in the state, not
    in-memory only): after restart the report still shows TWO
    unprotected shares (not zero, not reset), with the same exposure
    start. A partial exit fill then reduces that count by EXACTLY the
    quantity sold: 1 fills -> unprotected 2 -> 1, held 2 -> 1,
    exit-pending 2 -> 1. Each share's exposure interval ends when it
    is sold OR protection is confirmed restored."""
    home = tmp_path / "home"
    fixture = tmp_path / "fixture"
    home.mkdir(parents=True)
    limit, entry_price = _buy_fill_resize(home, fixture)
    log2 = _run_exit(home, fixture)
    assert _order_count(fixture) == 2  # buy + exit
    sell = next(
        i for i in _read_intents(home)
        if i["symbol"] == "NVDA" and i["side"] == "sell")
    assert sell["status"] == "SUBMITTED" and sell["qty"] == 2.0
    protection2 = _protection(log2, "NVDA")
    assert protection2 is not None
    assert protection2["held"] == 2.0
    assert protection2["exit_pending"] == 2.0
    assert protection2["protected"] == 0.0
    assert protection2["unprotected"] == 2.0
    started_at = protection2["exposure_started_at"]
    assert started_at is not None
    # Run 3 (restart, the exit still unfilled): the durable record
    # survives — the same exposure start, still TWO unprotected
    # shares, and NO duplicate exit submitted.
    log3 = _run_exit(home, fixture)
    assert _order_count(fixture) == 2
    protection3 = _protection(log3, "NVDA")
    assert protection3 is not None
    assert protection3["held"] == 2.0
    assert protection3["exit_pending"] == 2.0
    assert protection3["protected"] == 0.0
    assert protection3["unprotected"] == 2.0  # not zero, not reset
    assert protection3["exposure_started_at"] == started_at
    # A partial exit fill of 1 share reduces the count by EXACTLY 1.
    store = FileBackedFakeBroker(fixture)
    store.fill(sell["intent_id"], "fill-exit-1", 1, 160.00)
    log4 = _run_exit(home, fixture)
    assert _order_count(fixture) == 2  # still no duplicate exit
    protection4 = _protection(log4, "NVDA")
    assert protection4 is not None
    assert protection4["held"] == 1.0        # 2 -> 1
    assert protection4["exit_pending"] == 1.0  # 2 -> 1
    assert protection4["protected"] == 0.0
    assert protection4["unprotected"] == 1.0  # 2 -> 1, exactly the qty sold
    assert protection4["exposure_started_at"] == started_at
    # Fill the remaining share: the exit completes, the exposure
    # interval ends ("sold"), and the durable record is removed.
    store = FileBackedFakeBroker(fixture)
    store.fill(sell["intent_id"], "fill-exit-2", 1, 160.00)
    log5 = _run_exit(home, fixture)
    assert "paper:NVDA" not in _strategy_state(home).get("pending_exits", {})
    protection5 = _protection(log5, "NVDA")
    assert protection5 is not None
    assert protection5["held"] == 0.0
    assert protection5["exposure_end_reason"] == "sold"
    assert _total_sold(fixture, "NVDA") == 2.0


# ---------------------------------------------------------------- oversized stop (Detail 3)

def test_oversized_stop_before_fix_no_cap():
    """Detail 3, BEFORE the fix (same no-cap fake broker): entry qty
    2, only 1 share filled, stop attached at qty 2 (the entry qty).
    Trigger the stop before any resize. With no cap, the stop sells
    its FULL nominal qty (2) against 1 held share, leaving position
    -1 — a visible short. This is the failure the fix removes."""
    broker = FakeBroker()
    broker.set_stop_cap_disabled(True)
    broker.submit({
        "symbol": "NVDA", "qty": 2, "side": "buy", "type": "limit",
        "limit_price": 167.25, "time_in_force": "day",
        "client_order_id": "buy-1"})
    broker.apply_partial_fill("buy-1", "fill-1", 1, 167.25)  # only 1 of 2 filled
    # The in-memory FakeBroker has no positions() (that's the leg's
    # method): net the fills from its orders + stops.
    def _net(broker, symbol):
        total = 0.0
        for o in broker.list_orders() + broker.list_stops():
            if o.get("symbol") != symbol:
                continue
            total += float(o.get("filled_qty") or 0.0) * (1 if o.get("side") == "buy" else -1)
        return total
    assert _net(broker, "NVDA") == 1.0
    stop = broker.attach_trailing_stop("NVDA", 2.0, 0.06,
                                       client_order_id="stop-oversized")
    assert stop["qty"] == 2.0  # oversized: the entry qty, not the filled qty
    triggered = broker.trigger_trailing_stop("stop-oversized", 160.00)
    assert triggered["qty"] == 2.0  # the full nominal qty, uncapped
    assert _net(broker, "NVDA") == -1.0  # the short is visible
    assert broker.get_by_client_order_id("stop-oversized")["filled_qty"] == 2.0


def test_oversized_stop_after_fix_no_cap(tmp_path):
    """Detail 3 + follow-up 3, AFTER the fix (the SAME no-cap fake
    broker — the state file proves it: stop_cap_disabled is True).
    At attach the stop is sized to the FILLED qty only (1), not the
    entry qty (2). When the 2nd share fills, the OLD stop is
    cancelled (confirmed by order-ID lookup, not by absence from
    open orders) and a new stop is attached sized to the new filled
    qty (2). The resize order is cancel-old-then-attach-new, and at
    EACH step the per-step table reports (a) the total quantity the
    working stops could sell if they all triggered and (b) the shares
    actually held — sellable never exceeds held (no double
    protection). The cost of cancel-first is a brief unprotected
    window between the old stop's cancel confirmation and the new
    stop's attach, recorded explicitly."""
    home = tmp_path / "home"
    fixture = tmp_path / "fixture"
    home.mkdir(parents=True)
    # Arm the no-cap broker BEFORE Run 1 (persisted in the state
    # file, so the same broker runs the whole trace).
    _write_fixture(fixture)
    store = FileBackedFakeBroker(fixture)
    store.set_stop_cap_disabled(True)
    assert _broker_state(fixture)["stop_cap_disabled"] is True
    _, coid, limit = _run_buy(home, fixture, exit_on_signal_loss=1)
    store = FileBackedFakeBroker(fixture)
    store.fill(coid, "fill-1", 1, limit)  # only 1 of 2 filled
    _reset_submitted_today(home)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"],
                        exit_on_signal_loss=1)
    assert rc == 0, out
    log2 = _latest_decision_log(home)
    assert log2["status"] == "ok", out
    # At attach: the stop is sized to the FILLED qty (1), not the
    # entry qty (2).
    stop_coid = _stop_coid_for(fixture, "NVDA")
    assert _stops(fixture)[stop_coid]["qty"] == 1.0
    assert _held(fixture, "NVDA") == 1.0
    attached = _stop_sync(log2, "stop_attached")
    assert attached is not None and attached["new_stop_qty"] == 1.0
    trace = attached["resize_trace"]
    assert [t["sellable_qty"] for t in trace] == [0.0, 1.0]
    assert [t["held_qty"] for t in trace] == [1.0, 1.0]
    assert all(t["sellable_qty"] <= t["held_qty"] for t in trace)
    # The 2nd share fills: the old stop must be cancelled (confirmed
    # by order-id lookup) and a new stop attached sized to 2.
    old_coid = stop_coid
    old_stop_id = _strategy_state(home)["open_entries"]["NVDA"]["trail_stop_id"]
    store = FileBackedFakeBroker(fixture)
    store.fill(coid, "fill-2", 1, limit)
    assert _held(fixture, "NVDA") == 2.0
    _reset_submitted_today(home)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"],
                        exit_on_signal_loss=1)
    assert rc == 0, out
    log3 = _latest_decision_log(home)
    assert log3["status"] == "ok", out
    # The old stop's cancellation was confirmed BY ORDER ID (not by
    # its absence from open orders): the resize only attaches the new
    # stop after that confirmation — an unconfirmed cancel DEFERS the
    # resize (action "stop_resize_deferred") instead. The new stop is
    # a NEW broker order (a new id) under the same deterministic
    # coid, sized to the new filled qty (2) — the old stop's record
    # under that coid is replaced by the new one, so the confirmation
    # is evidenced by the trace step + the new id, not by reading
    # the old stop's status from the coid-keyed dict.
    new_stop_id = _strategy_state(home)["open_entries"]["NVDA"]["trail_stop_id"]
    assert new_stop_id != old_stop_id
    new_coid = _stop_coid_for(fixture, "NVDA")
    assert new_coid == old_coid  # the deterministic coid is reused
    assert _stops(fixture)[new_coid]["qty"] == 2.0
    assert _stops(fixture)[new_coid]["status"] == "submitted"
    assert _stops(fixture)[new_coid]["id"] == new_stop_id
    resized = _stop_sync(log3, "stop_resized")
    assert resized is not None
    assert resized["old_stop_qty"] == 1.0 and resized["new_stop_qty"] == 2.0
    trace = resized["resize_trace"]
    # Per-step table: (a) total sellable qty of all still-working
    # stops, (b) shares held. Cancel-old-then-attach-new: the cancel
    # step comes BEFORE the attach step, so sellable never exceeds
    # held (no double-protection window) — the cost is the
    # unprotected gap between the cancel confirmation and the attach.
    actions = [t["action"] for t in trace]
    assert actions == ["resize_start_old_stop_working",
                       "old_stop_cancel_confirmed", "new_stop_attached"]
    assert actions.index("old_stop_cancel_confirmed") < actions.index("new_stop_attached")
    for step in trace:
        assert step["sellable_qty"] <= step["held_qty"], step
    assert [t["held_qty"] for t in trace] == [2.0] * len(trace)
    assert trace[0]["sellable_qty"] == 1.0  # the old stop, still working
    assert trace[-1]["sellable_qty"] == 2.0  # the new stop, attached
    assert trace[-1]["unprotected_shares"] == 0.0
    gap = [t for t in trace if t["action"] == "old_stop_cancel_confirmed"][0]
    assert gap["unprotected_shares"] == 2.0  # the brief unprotected gap
    window = resized["unprotected_window"]
    assert window["start"] < window["end"]
    assert window["uncertain"] is False  # both ends are known times
    # No short ever appeared, even with the cap off: the position
    # stayed >= 0 throughout.
    assert _held(fixture, "NVDA") == 2.0


# ---------------------------------------------------------------- B17.5 (live leg)

def test_b175_live_entry_fail_no_duplicate(tmp_path):
    """B17.5 e2e: the live entry's response is dropped (fail before
    storing — never accepted). The live intent is UNKNOWN and NO
    duplicate live submission happens on retry: the next run retries
    the LOOKUP only, the intent stays UNKNOWN, and the live store
    still holds 0 orders. The paper entry is unaffected."""
    home = tmp_path / "home"
    fixture = tmp_path / "fixture"
    home.mkdir(parents=True)
    _write_fixture(fixture)
    _set_live_enabled(fixture)
    live_store = _live_store(fixture)
    live_store.fail_next_submit()
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"],
                        exit_on_signal_loss=1)
    assert rc == 0, out
    log1 = _latest_decision_log(home)
    assert log1["status"] == "ok", out
    paper = next(i for i in _read_intents(home) if i["side"] == "buy")
    assert paper["status"] == "SUBMITTED"
    assert _live_order_count(fixture) == 0  # never accepted: no live order
    # The live intent (account=live) is UNKNOWN, attempts 1.
    from order_manager import OrderManager
    mgr = OrderManager(str(home / "order_intents.db"))
    live_rows = [r for r in mgr.unresolved_intents(account="live")]
    assert len(live_rows) == 1
    assert live_rows[0]["status"] == "UNKNOWN"
    assert live_rows[0]["attempts"] == 1
    assert live_rows[0]["broker_order_id"] is None
    # Run 2: the retry is lookup-only — no second live submission.
    _reset_submitted_today(home)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"],
                        exit_on_signal_loss=1)
    assert rc == 0, out
    assert _live_order_count(fixture) == 0
    live_rows = [r for r in mgr.unresolved_intents(account="live")]
    assert len(live_rows) == 1 and live_rows[0]["status"] == "UNKNOWN"
    assert live_rows[0]["attempts"] == 1  # no new attempt
    assert _strategy_state(home)["open_entries"]["NVDA"]["qty"] == 2.0


def test_b175_live_entry_drop_adopted_no_duplicate(tmp_path):
    """B17.5 e2e: the live entry is accepted but its response is
    dropped. The manager's post-submit lookup adopts it (SUBMITTED),
    and the next run does not duplicate it: the live store still
    holds exactly 1 order."""
    home = tmp_path / "home"
    fixture = tmp_path / "fixture"
    home.mkdir(parents=True)
    _write_fixture(fixture)
    _set_live_enabled(fixture)
    live_store = _live_store(fixture)
    live_store.set_drop_next_response(True)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"],
                        exit_on_signal_loss=1)
    assert rc == 0, out
    log1 = _latest_decision_log(home)
    assert log1["status"] == "ok", out
    assert _live_order_count(fixture) == 1  # accepted, response dropped
    from order_manager import OrderManager
    mgr = OrderManager(str(home / "order_intents.db"))
    live_intents = [r for r in mgr.unresolved_intents(account="live")]
    assert live_intents == []  # adopted by the lookup, not UNKNOWN
    _reset_submitted_today(home)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"],
                        exit_on_signal_loss=1)
    assert rc == 0, out
    assert _live_order_count(fixture) == 1  # no duplicate


def test_b175_live_exit_fail_no_duplicate(tmp_path):
    """B17.5 e2e: the live exit's response is dropped (fail before
    storing). The live exit intent is UNKNOWN, the paper exit still
    submits, and the next run retries the LOOKUP only — no duplicate
    live exit is submitted."""
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
    # Fill both legs' buys (the paper buy via the paper store; the
    # live buy via the live store — one intent per account).
    paper_store = FileBackedFakeBroker(fixture)
    paper_store.fill(_open_order_coid(fixture, "NVDA"), "fill-1", 2, limit)
    # The live buy's client_order_id is the live intent id; find it
    # in the live store's orders.
    live_store = _live_store(fixture)
    live_orders = live_store.list_all_orders()
    assert len(live_orders) == 1
    live_store.fill(live_orders[0]["client_order_id"], "fill-live-1", 2, limit)
    _reset_submitted_today(home)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"],
                        exit_on_signal_loss=1)
    assert rc == 0, out
    # The resize run attached the paper stop; the live leg has no
    # stop (paper-only sizing) — the live exit cancels nothing.
    live_store = _live_store(fixture)
    live_store.fail_next_submit()
    log2 = _run_exit(home, fixture, live_enabled=True)
    assert _order_count(fixture) == 2  # paper buy + paper exit
    assert _live_order_count(fixture) == 1  # live buy only: no live exit stored
    from order_manager import OrderManager
    mgr = OrderManager(str(home / "order_intents.db"))
    live_rows = [r for r in mgr.unresolved_intents(account="live")]
    assert len(live_rows) == 1
    assert live_rows[0]["status"] == "UNKNOWN"
    assert live_rows[0]["attempts"] == 1
    # The live pending-exit record is durable (exit_unknown).
    pending = _strategy_state(home)["pending_exits"].get("live:NVDA")
    assert pending is not None and pending["exit_unknown"] is True
    started_at = pending["exposure_started_at"]
    # Run 3: the retry is lookup-only — no duplicate live exit.
    log3 = _run_exit(home, fixture, live_enabled=True)
    assert _live_order_count(fixture) == 1
    live_rows = [r for r in mgr.unresolved_intents(account="live")]
    assert len(live_rows) == 1 and live_rows[0]["status"] == "UNKNOWN"
    assert live_rows[0]["attempts"] == 1
    pending = _strategy_state(home)["pending_exits"].get("live:NVDA")
    assert pending is not None
    assert pending["exposure_started_at"] == started_at


# ---------------------------------------------------------------- B17.6

def test_b176_reservation_prices_from_filled_avg():
    """B17.6 (consumer): a DONE_FOR_DAY market buy's reservation
    prices from its filled average — the price the real leg's lookup
    now returns — instead of last_price / entry_price. The leg-level
    tests (the two updated B13 lookup tests) assert the keys are
    present on both the SDK and REST paths and that a 404 still
    returns None."""
    market_buy = {
        "symbol": "NVDA", "side": "buy", "qty": 2, "filled_qty": 1,
        "limit_price": None, "filled_avg_price": 167.20,
    }
    price, source = _reservation_price_for_order(
        market_buy, last_prices={"NVDA": 170.00}, entry_prices={"NVDA": 165.00})
    assert (price, source) == (167.20, "filled_avg_price")
    # Without a filled average, the chain falls back to last_price.
    no_avg = dict(market_buy, filled_avg_price=None)
    price, source = _reservation_price_for_order(
        no_avg, last_prices={"NVDA": 170.00}, entry_prices={"NVDA": 165.00})
    assert (price, source) == (170.00, "last_price")
    # A limit buy still prices from its limit (the worst-case cost).
    limit_buy = dict(market_buy, limit_price=167.25)
    price, source = _reservation_price_for_order(
        limit_buy, last_prices={"NVDA": 170.00}, entry_prices={"NVDA": 165.00})
    assert (price, source) == (167.25, "limit_price")
