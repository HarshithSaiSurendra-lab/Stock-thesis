"""B13 — soak readiness part 1: B3 wired into the live loop.

Build 2 (this file's core): an end-to-end fault-injection test through
main.py as a SUBPROCESS — the normal daily decision entry point — with
dated fixtures and a fake broker. Sequence:
  Run 0  dry-run preview (main.py --dry-run): one order, zero submissions.
  Run 1  live run, broker accepts + drops the response, lookups delayed
         -> intent UNKNOWN + alert + symbol BLOCKED. Exactly 1 attempt.
  Run 2  fresh process, same intent store, lookups still inconclusive
         -> startup reconcile keeps the intent UNKNOWN, the strategy's
         conflicting order is refused as BLOCKED, zero extra attempts.
  Run 3  lookups now return the original order -> runner adopts it,
         alert resolves, block clears. Still exactly 1 attempt, 1 order.

Unit tests alongside: _live_intent determinism, _submit_paper_via_manager
outcomes (accepted / dropped / rejected / blocked), and
AlpacaLeg.get_by_client_order_id (SDK + REST + 404).

Everything offline and deterministic: the fake broker is fixture-driven
(never the real Alpaca adapter — Alpaca keys are scrubbed from the
subprocess env and cwd is a temp dir, so load_dotenv() finds no .env),
ALLOW_LIVE=0, and the live leg is asserted to never place an order.

Timing guards: the fixtures are dated to the latest completed NYSE
session, so a run that crosses a session close (16:00 ET) or UTC
midnight (the intent's trading_day) mid-sequence would change the
inputs underneath the test. Both are guarded with a skip (the window
is ~3 minutes; the sequence itself takes well under a minute).
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

from fake_broker import FakeBroker  # noqa: E402
from fake_dual_broker import FakeDataClient  # noqa: E402
from order_manager import OrderManager, build_intent, compute_inputs_hash, derive_client_order_id  # noqa: E402
from strategy_runner import LIVE_DESIGN_ID, _live_intent, _submit_paper_via_manager  # noqa: E402

VENV_PY = REPO / ".venv" / "bin" / "python"

# ---------------------------------------------------------------- fixtures

N_SESSIONS = 330


def _sessions() -> list:
    """Last N_SESSIONS NYSE sessions ending on the latest completed
    session (the same freshness rule the daily run uses)."""
    import pandas_market_calendars as mcal

    cal = mcal.get_calendar("NYSE")
    now = datetime.now(timezone.utc)
    # Latest completed session: the last session whose close <= now.
    schedule = cal.schedule(start_date=now.date() - timedelta(days=800), end_date=now.date())
    closes = schedule["market_close"]
    completed = [d for d, close in closes.items() if close.tz_convert(timezone.utc) <= now]
    assert completed, "no completed NYSE session found"
    last = completed[-1]
    sessions = cal.schedule(start_date=last - timedelta(days=800), end_date=last).index
    return list(sessions[-N_SESSIONS:])


def _frame(start: float, cycle: tuple[float, ...], vol: float, sessions: list) -> dict:
    """Synthetic OHLCV frame: geometric steps cycling through `cycle`
    (day-over-day returns), with a tiny sine wiggle so the window is
    not degenerate. Mirrors the verified probe (SPY +0.12%/day,
    NVDA +1.6/+1.6/-1.6, XLK +/-0.8)."""
    import math

    closes: list[float] = []
    price = start
    for i in range(len(sessions)):
        price *= 1.0 + cycle[i % len(cycle)]
        closes.append(price * (1.0 + 0.0015 * math.sin(i / 3.0)))
    opens, highs, lows, vols = [], [], [], []
    for i, session in enumerate(sessions):
        close = closes[i]
        prev = closes[i - 1] if i else close
        opens.append(prev)
        highs.append(close * 1.005)
        lows.append(close * 0.995)
        vols.append(vol)
    # Column-oriented, as FakeDataClient expects: {symbol: {"open": [...],
    # ..., "index": [dates]}}.
    return {"index": [s.strftime("%Y-%m-%d") for s in sessions],
            "open": opens, "high": highs, "low": lows,
            "close": closes, "volume": vols}


def _write_fixture(fixture_dir: Path, *, nvda_cycle=(0.016, 0.016, -0.016),
                   drop: bool = False, polls: int = 0) -> None:
    """Write bars.json / quotes.json / broker_config.json. Never touches
    broker_state.json — the fake broker's memory persists across runs
    (that's the point of the fault-injection sequence)."""
    fixture_dir.mkdir(parents=True, exist_ok=True)
    sessions = _sessions()
    frames = {
        "SPY": _frame(400.0, (0.0012,), 80_000_000, sessions),
        "NVDA": _frame(30.0, nvda_cycle, 5_000_000, sessions),
        "XLK": _frame(150.0, (0.008, -0.008), 20_000_000, sessions),
    }
    (fixture_dir / "bars.json").write_text(json.dumps(frames))
    quotes = {}
    for sym, frame in frames.items():
        last = frame["close"][-1]
        quotes[sym] = {"bid_offset": -0.01, "ask_offset": 0.01, "last": last}
    (fixture_dir / "quotes.json").write_text(json.dumps(quotes))
    (fixture_dir / "broker_config.json").write_text(json.dumps({
        "paper_equity": 100_000.0,
        "drop_next_response": drop,
        "delayed_visibility_polls": polls,
    }))


# ---------------------------------------------------------------- harness

_SCRUB = (
    "ALPACA_PAPER_KEY", "ALPACA_PAPER_SECRET", "ALPACA_LIVE_KEY", "ALPACA_LIVE_SECRET",
    "NOTIFIER_TOKEN", "NOTIFIER_CHAT_ID", "ORDER_STORE_PATH", "STRATEGY_STATE_PATH",
    "GUARDIAN_STATE_PATH", "BROKER_STATE_PATH", "MEMORY_DB_PATH",
    "EXECUTION_JOURNAL_DB_PATH", "ALERT_LOG_PATH", "KILL_SWITCH_PATH",
    "DECISION_LOG_DIR", "BAR_CACHE_DIR", "BAR_CACHE_ENABLED",
    "EXIT_ON_SIGNAL_LOSS", "UNIVERSE_SEED_SYMBOLS",
)


def _env(home: Path, fixture_dir: Path, *, exit_on_signal_loss: int) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
    env.update({
        "ATS_FAKE_BROKER_DIR": str(fixture_dir),
        "ALLOW_LIVE": "0",
        "BAR_CACHE_ENABLED": "0",
        "EXIT_ON_SIGNAL_LOSS": str(exit_on_signal_loss),
        "ORDER_STORE_PATH": str(home / "order_intents.db"),
        "STRATEGY_STATE_PATH": str(home / "strategy_state.json"),
        "GUARDIAN_STATE_PATH": str(home / "guardian_state.json"),
        "BROKER_STATE_PATH": str(home / "broker_state.json"),
        "MEMORY_DB_PATH": str(home / "memory.db"),
        "EXECUTION_JOURNAL_DB_PATH": str(home / "execution_journal.db"),
        "ALERT_LOG_PATH": str(home / "alerts.log"),
        "KILL_SWITCH_PATH": str(home / "kill_switch"),
        "DECISION_LOG_DIR": str(home / ".cache" / "decision_logs"),
        "BAR_CACHE_DIR": str(home / ".cache" / "bars"),
        "PYTHONPATH": str(REPO),
    })
    return env


def _run_main(home: Path, fixture_dir: Path, args: list[str], *,
              exit_on_signal_loss: int = 0) -> tuple[int, str]:
    """Run main.py as a subprocess with cwd=home (so load_dotenv finds
    no real .env) and Alpaca keys scrubbed. Returns (rc, combined
    stdout+stderr)."""
    proc = subprocess.run(
        [str(VENV_PY), str(REPO / "main.py"), *args],
        cwd=str(home),
        env=_env(home, fixture_dir, exit_on_signal_loss=exit_on_signal_loss),
        capture_output=True, text=True, timeout=300,
    )
    return proc.returncode, proc.stdout + proc.stderr


def _latest_decision_log(home: Path) -> dict:
    logs = sorted((home / ".cache" / "decision_logs").glob("*.json"),
                  key=lambda p: p.stat().st_mtime)
    assert logs, "no decision log written"
    return json.loads(logs[-1].read_text())


def _read_intents(home: Path) -> list[dict]:
    db = home / "order_intents.db"
    assert db.exists(), "order_intents.db missing"
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT intent_id, design_id, trading_day, symbol, side, qty,"
            " order_type, limit_price, status, broker_order_id, attempts"
            " FROM intents ORDER BY rowid")]


def _read_alerts(home: Path) -> list[dict]:
    with sqlite3.connect(home / "order_intents.db") as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT intent_id, reason, resolved FROM alerts ORDER BY rowid")]


def _broker_state(fixture_dir: Path) -> dict:
    return json.loads((fixture_dir / "broker_state.json").read_text())


def _order_count(state: dict) -> int:
    return len(state.get("orders", {}))


def _expected_intent_id(preview: dict, trading_day: str) -> str:
    """Recompute the intent id the wiring should derive from the
    preview order — proving the intent comes from the SAME order dict
    the dry-run preview builds (design id, inputs, prices rounded
    exactly as _live_intent rounds them)."""
    order = preview["order"]
    inputs = {
        "symbol": preview["symbol"],
        "direction": preview["direction"],
        "decision_score": round(float(preview["decision_score"]), 6),
        "score": round(float(preview["score"]), 6),
        "trend_quality": round(float(preview["trend_quality"]), 6),
        "last_price": round(float(preview["last_price"]), 2),
        "target_notional": round(float(preview["target_notional"]), 2),
        "intended_notional": round(float(preview["intended_notional"]), 2),
    }
    return derive_client_order_id(
        LIVE_DESIGN_ID, trading_day, preview["symbol"],
        order["side"], float(order["qty"]), order["order_type"],
        float(order["limit_price"]) if order.get("limit_price") else None,
        compute_inputs_hash(inputs),
    )


def _guard_timing() -> None:
    """Skip if a session close or UTC midnight falls inside the next
    3 minutes: either would change the fixture's latest session or
    the intent's trading_day mid-sequence."""
    now = datetime.now(timezone.utc)
    if (now + timedelta(minutes=3)).date() != now.date():
        pytest.skip("UTC midnight within 3 min — fixtures would change mid-run")
    import pandas_market_calendars as mcal

    cal = mcal.get_calendar("NYSE")
    schedule = cal.schedule(start_date=now.date(), end_date=now.date() + timedelta(days=5))
    if schedule.empty:
        return
    next_close = schedule["market_close"].min().tz_convert(timezone.utc).to_pydatetime()
    if next_close - now < timedelta(minutes=3):
        pytest.skip("NYSE session close within 3 min — fixtures would change mid-run")


def _reset_decision_dedupe(home: Path) -> None:
    """Clear submitted_today + open_entries from the strategy state
    file only, keeping expected_positions. This simulates the runner
    re-deriving the same decision from the same fixtures after a
    restart (the B3 design case). A full state wipe would instead
    (correctly) trip the end-of-run reconcile drift halt, because
    the broker still holds the accepted position — a different
    scenario (state loss), not the restart case."""
    path = home / "strategy_state.json"
    if not path.exists():
        return
    state = json.loads(path.read_text())
    state["submitted_today"] = []
    state["open_entries"] = {}
    path.write_text(json.dumps(state))


# ---------------------------------------------------------------- e2e

def test_e2e_fault_injection_through_main(tmp_path):
    """Runs 0-3 as described in the module docstring. The load-bearing
    assertions: attempt counts 1/1/1 across the three live runs, the
    alert/block/adopt/clear transitions, and the submitted order
    EQUALLING the dry-run preview order."""
    _guard_timing()
    home = tmp_path / "home"
    fixture = tmp_path / "fixture"
    home.mkdir(parents=True)
    _write_fixture(fixture, drop=True, polls=6)

    # --- Run 0: dry-run preview (the decision the live run must repeat).
    rc, out = _run_main(home, fixture, ["--dry-run", "--mode", "regular", "--broker", "fake"])
    assert rc == 0, out
    log0 = _latest_decision_log(home)
    assert log0["status"] == "dry_run", out
    assert len(log0["orders"]) == 1, log0
    preview = log0["orders"][0]
    assert preview["symbol"] == "NVDA" and preview["direction"] == "mild_up"
    assert preview["order"]["side"] == "buy"
    assert not (home / "order_intents.db").exists(), "dry run must not touch the intent store"
    assert not (fixture / "broker_state.json").exists(), "dry run must not touch the broker"

    # --- Run 1: broker accepts + drops the response, lookups delayed.
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"])
    assert rc == 0, out
    log1 = _latest_decision_log(home)
    assert log1["status"] == "ok", out
    assert len(log1["orders"]) == 1, log1
    assert log1["orders"][0]["order"] == preview["order"], "submitted order must equal the preview order"
    intents = _read_intents(home)
    assert len(intents) == 1, intents
    intent = intents[0]
    assert intent["intent_id"] == _expected_intent_id(preview, log1["date"]), "intent derived from the preview order"
    assert intent["status"] == "UNKNOWN" and intent["attempts"] == 1 and intent["broker_order_id"] is None
    alerts = _read_alerts(home)
    assert len(alerts) == 1 and alerts[0]["resolved"] == 0, alerts
    state1 = _broker_state(fixture)
    assert _order_count(state1) == 1 and state1["submit_calls"] == 1, state1
    assert state1["live_submit_calls"] == 1, state1
    assert list(state1["orders"].values())[0]["symbol"] == "NVDA", state1
    assert state1["drop_next_response"] is False, "the drop was consumed exactly once"

    # --- Run 2: fresh process, lookups STILL inconclusive (6 polls
    # consumed: 3 in Run 1's submit-reconcile + 3 here) -> refused.
    _reset_decision_dedupe(home)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"])
    assert rc == 0, out
    log2 = _latest_decision_log(home)
    assert log2["status"] == "ok", out
    assert log2["orders"] == [], "blocked run must submit nothing"
    assert any(
        s.get("stage") == "order_manager" and "blocked by unresolved intent" in s.get("reason", "")
        for s in log2.get("skipped", [])
    ), log2.get("skipped")
    assert any(
        key.startswith("order_manager:") and "blocked by unresolved intent" in key
        for key in log2.get("skip_summary", {})
    ), log2.get("skip_summary")
    intents = _read_intents(home)
    assert len(intents) == 1 and intents[0]["status"] == "UNKNOWN"
    assert intents[0]["attempts"] == 1, "no second attempt was made"
    assert intents[0]["broker_order_id"] is None
    alerts = _read_alerts(home)
    assert len(alerts) == 1 and alerts[0]["resolved"] == 0, "alert persists"
    state2 = _broker_state(fixture)
    assert _order_count(state2) == 1 and state2["submit_calls"] == 1, state2
    assert state2["live_submit_calls"] == 1, "the blocked run never reached the live leg"

    # --- Run 3: polls exhausted -> lookup returns the original order.
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"])
    assert rc == 0, out
    log3 = _latest_decision_log(home)
    assert log3["status"] == "ok", out
    assert len(log3["orders"]) == 1, log3
    assert log3["orders"][0]["order"] == preview["order"]
    intents = _read_intents(home)
    assert len(intents) == 1, intents
    assert intents[0]["status"] == "SUBMITTED" and intents[0]["attempts"] == 1
    assert intents[0]["broker_order_id"] == "fake-0001", "adopted the ORIGINAL order"
    alerts = _read_alerts(home)
    assert len(alerts) == 1 and alerts[0]["resolved"] == 1, "alert resolved on adoption"
    state3 = _broker_state(fixture)
    assert _order_count(state3) == 1 and state3["submit_calls"] == 1, "still exactly one attempt, one order"
    assert state3["live_submit_calls"] == 2, state3
    # B17 / Detail 3: the adopted order is UNFILLED, so no trailing
    # stop is attached at entry time — the stop is sized to the
    # FILLED qty (the protective quantity), not the entry qty, and
    # nothing is held yet. The startup size sync attaches it when the
    # first fill arrives. The adopted order still flows through the
    # normal post-submit path (the intent / order assertions above).
    assert len(state3["trail_calls"]) == 0, (
        "an unfilled adopted order gets no stop yet (B17: stop sized to filled qty)")


def test_exit_goes_through_manager_after_entry(tmp_path):
    """Entry on a rising fixture, then exit on a falling fixture with
    exit_on_signal_loss=1: the exit is a second intent through
    OrderManager (a second submission path that must not double-submit
    after a timeout either)."""
    _guard_timing()
    home = tmp_path / "home"
    fixture = tmp_path / "fixture"
    home.mkdir(parents=True)
    _write_fixture(fixture, drop=False, polls=0)

    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"],
                        exit_on_signal_loss=1)
    assert rc == 0, out
    log_a = _latest_decision_log(home)
    assert log_a["status"] == "ok" and len(log_a["orders"]) == 1, out
    intents = _read_intents(home)
    assert len(intents) == 1 and intents[0]["status"] == "SUBMITTED"
    assert intents[0]["broker_order_id"] == "fake-0001"

    # NVDA now falls: signal lost, exit_on_signal_loss exits the open entry.
    # B16: the exit cancels the working buy FIRST and sells only the
    # filled qty, so fill 1 of the 2 shares before the exit (an
    # unfilled buy would exit with no sell at all).
    from fake_dual_broker import FileBackedFakeBroker
    entry_order = log_a["orders"][0]["order"]
    # The intent id IS the client_order_id (the preview order itself
    # carries no client_order_id — the manager attaches it at submit).
    entry_coid = _read_intents(home)[0]["intent_id"]
    FileBackedFakeBroker(fixture).fill(
        entry_coid, "fill-1", 1, entry_order["limit_price"])
    _write_fixture(fixture, nvda_cycle=(-0.016, -0.016, 0.016), drop=False, polls=0)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"],
                        exit_on_signal_loss=1)
    assert rc == 0, out
    log_b = _latest_decision_log(home)
    assert log_b["status"] == "ok", out
    assert log_b["orders"] == [], "no new entry while exiting"
    intents = _read_intents(home)
    assert len(intents) == 2, intents
    entry, exit_intent = intents
    # The exit sells only the filled share (B16), not the entry qty.
    assert exit_intent["side"] == "sell" and exit_intent["qty"] == 1.0
    assert exit_intent["status"] == "SUBMITTED" and exit_intent["broker_order_id"] == "fake-0002"
    assert exit_intent["attempts"] == 1
    state = _broker_state(fixture)
    assert _order_count(state) == 2 and state["submit_calls"] == 2, state
    assert state["live_submit_calls"] == 2, state
    # The buy was cancelled by the exit (not left working).
    buy = [o for o in state["orders"].values() if o["side"] == "buy"][0]
    assert buy["status"] == "cancelled" and buy["filled_qty"] == 1.0
    with open(home / "strategy_state.json") as fh:
        strategy_state = json.load(fh)
    assert strategy_state["open_entries"] == {}, "entry closed after the exit"


# ---------------------------------------------------------------- reconcile
# B13 fallout: reconcile() counts open-order unfilled quantity toward the
# actual holding, because positions() only reflects fills. These tests
# pin that semantic with stub legs (no broker, no network).

def _reconcile_cfg(tmp_path, expected: dict):
    from config import TradingConfig

    cfg = TradingConfig()
    state_path = tmp_path / "strategy_state.json"
    state_path.write_text(json.dumps({"expected_positions": expected}))
    cfg.paths.strategy_state_path = str(state_path)
    return cfg


def _stub_leg(positions=None, open_orders=None):
    return SimpleNamespace(
        positions=lambda: dict(positions or {}),
        open_orders=lambda: list(open_orders or []),
    )


def _stub_broker(paper=None, live=None):
    return SimpleNamespace(
        paper=paper or _stub_leg(),
        live=live or _stub_leg(),
    )


def test_reconcile_unfilled_open_order_covers_expected(tmp_path):
    """The live-loop case: a limit order submitted but not yet filled.
    Expected 3, no filled position, one open buy of 3 -> ok."""
    from reconcile import reconcile

    cfg = _reconcile_cfg(tmp_path, {"paper": {"NVDA": {"qty": 3}}})
    broker = _stub_broker(paper=_stub_leg(
        open_orders=[{"symbol": "NVDA", "side": "buy", "qty": 3.0,
                      "filled_qty": 0.0, "status": "submitted"}]))
    result = reconcile(broker, None, cfg)
    assert result["ok"] is True, result
    assert result["actual"]["paper"]["NVDA"]["qty"] == 3.0
    assert result["actual"]["paper"]["NVDA"]["open_order_qty"] == 3.0


def test_reconcile_partial_fill_plus_open_order_covers_expected(tmp_path):
    from reconcile import reconcile

    cfg = _reconcile_cfg(tmp_path, {"paper": {"NVDA": {"qty": 3}}})
    broker = _stub_broker(paper=_stub_leg(
        positions={"NVDA": {"qty": 1.0}},
        open_orders=[{"symbol": "NVDA", "side": "buy", "qty": 3.0,
                      "filled_qty": 1.0, "status": "partially_filled"}]))
    result = reconcile(broker, None, cfg)
    assert result["ok"] is True, result
    assert result["actual"]["paper"]["NVDA"]["qty"] == 3.0


def test_reconcile_expected_missing_with_no_open_order_is_drift(tmp_path):
    """The check is not weakened: expected 3, nothing filled, no open
    order -> discrepancy (the order vanished)."""
    from reconcile import reconcile

    cfg = _reconcile_cfg(tmp_path, {"paper": {"NVDA": {"qty": 3}}})
    result = reconcile(_stub_broker(), None, cfg)
    assert result["ok"] is False, result
    assert any("NVDA" in d and "expected 3" in d for d in result["discrepancies"])


def test_reconcile_unexpected_open_order_is_drift(tmp_path):
    """An open order the strategy never placed is drift, same as an
    unexpected position."""
    from reconcile import reconcile

    cfg = _reconcile_cfg(tmp_path, {"paper": {}})
    broker = _stub_broker(paper=_stub_leg(
        open_orders=[{"symbol": "AAPL", "side": "buy", "qty": 5.0,
                      "filled_qty": 0.0, "status": "submitted"}]))
    result = reconcile(broker, None, cfg)
    assert result["ok"] is False, result
    assert any("AAPL" in d and "unexpected" in d for d in result["discrepancies"])


def test_reconcile_unexpected_position_still_drift(tmp_path):
    from reconcile import reconcile

    cfg = _reconcile_cfg(tmp_path, {"paper": {}})
    broker = _stub_broker(paper=_stub_leg(positions={"AAPL": {"qty": 5.0}}))
    result = reconcile(broker, None, cfg)
    assert result["ok"] is False, result
    assert any("AAPL" in d and "unexpected" in d for d in result["discrepancies"])


def test_reconcile_buy_plus_sell_open_orders_net_to_zero(tmp_path):
    """Entry + exit both open and unfilled: net zero, no discrepancy."""
    from reconcile import reconcile

    cfg = _reconcile_cfg(tmp_path, {"paper": {}})
    broker = _stub_broker(paper=_stub_leg(open_orders=[
        {"symbol": "NVDA", "side": "buy", "qty": 3.0, "filled_qty": 0.0, "status": "submitted"},
        {"symbol": "NVDA", "side": "sell", "qty": 3.0, "filled_qty": 0.0, "status": "submitted"},
    ]))
    result = reconcile(broker, None, cfg)
    assert result["ok"] is True, result
    assert result["actual"]["paper"] == {}


def test_reconcile_leg_without_open_orders_behaves_as_before(tmp_path):
    """A leg with no open_orders() method (older adapters) falls back
    to positions-only: an unfilled order is drift, as before."""
    from reconcile import reconcile

    cfg = _reconcile_cfg(tmp_path, {"paper": {"NVDA": {"qty": 3}}})
    leg = SimpleNamespace(positions=lambda: {})
    result = reconcile(_stub_broker(paper=leg), None, cfg)
    assert result["ok"] is False, result


# ---------------------------------------------------------------- unit tests

def _fake_manager(tmp_path, fake: FakeBroker):
    mgr = OrderManager(str(tmp_path / "intents.db"))
    # FakeBroker dedupes by client_order_id, so order_count alone
    # cannot prove "exactly one attempt" — count the calls.
    calls: list[dict] = []

    def counting_submit(order: dict) -> dict:
        calls.append(order)
        return fake.submit(order)

    broker = SimpleNamespace(
        paper=SimpleNamespace(submit=counting_submit,
                              get_by_client_order_id=fake.get_by_client_order_id),
        live=SimpleNamespace(submit=lambda order: None),
    )
    return mgr, broker, calls


def _entry_order_and_intent():
    order = {
        "symbol": "NVDA", "qty": 3, "side": "buy", "type": "limit",
        "limit_price": 167.18, "time_in_force": "day",
        "client_order_id": "unused-here",
    }
    intent = _live_intent(order, {
        "symbol": "NVDA", "direction": "mild_up", "decision_score": 15.261,
        "score": 15.0, "trend_quality": 4.5, "last_price": 167.2,
        "target_notional": 603.36, "intended_notional": 501.56,
    }, "2026-11-26")
    return order, intent


def test_live_intent_deterministic_and_score_sensitive():
    order, intent_a = _entry_order_and_intent()
    _, intent_b = _entry_order_and_intent()
    assert intent_a == intent_b, "same order+inputs must derive the same intent"
    inputs = {"symbol": "NVDA", "direction": "mild_up", "decision_score": 15.262,
              "score": 15.0, "trend_quality": 4.5, "last_price": 167.2,
              "target_notional": 603.36, "intended_notional": 501.56}
    assert _live_intent(order, inputs, "2026-11-26") != intent_a
    assert intent_a["design_id"] == LIVE_DESIGN_ID and intent_a["account"] == "paper"


def test_submit_paper_via_manager_success(tmp_path):
    fake = FakeBroker()
    mgr, broker, calls = _fake_manager(tmp_path, fake)
    order, intent = _entry_order_and_intent()
    result = _submit_paper_via_manager(order, intent, mgr, broker)
    assert result == {"id": "fake-0001", "symbol": "NVDA", "status": "submitted",
                      "submitted": order}
    assert fake.order_count() == 1 and len(calls) == 1
    assert mgr.get_intent(intent["intent_id"])["status"] == "SUBMITTED"


def test_submit_paper_via_manager_drop_becomes_unknown(tmp_path):
    fake = FakeBroker(drop_next_response=True, delayed_visibility_polls=3)
    mgr, broker, calls = _fake_manager(tmp_path, fake)
    order, intent = _entry_order_and_intent()
    with pytest.raises(TimeoutError):
        _submit_paper_via_manager(order, intent, mgr, broker)
    assert fake.order_count() == 1 and len(calls) == 1
    row = mgr.get_intent(intent["intent_id"])
    assert row["status"] == "UNKNOWN" and row["attempts"] == 1
    assert mgr.is_blocked("NVDA", account="paper")


def test_submit_paper_via_manager_rejected_when_leg_returns_none(tmp_path):
    """A paper leg that returns None (disabled / floor / cap) is an
    explicit rejection for the manager, not an UNKNOWN: the order was
    NOT accepted, so there is nothing to recover and no block."""
    fake = FakeBroker()
    mgr, broker, calls = _fake_manager(tmp_path, fake)
    broker.paper.submit = lambda order: None
    order, intent = _entry_order_and_intent()
    assert _submit_paper_via_manager(order, intent, mgr, broker) is None
    assert fake.order_count() == 0
    row = mgr.get_intent(intent["intent_id"])
    assert row["status"] == "REJECTED"
    assert not mgr.is_blocked("NVDA", account="paper")


def test_submit_paper_via_manager_returns_none_when_blocked(tmp_path):
    """A DIFFERENT decision for the same symbol while the first intent
    is UNKNOWN is refused as BLOCKED (no broker call). A re-derived
    IDENTICAL decision is the same intent and instead re-reconciles
    it (adopting it once lookups succeed) — that is the B3 fast path,
    not a block."""
    fake = FakeBroker(drop_next_response=True, delayed_visibility_polls=3)
    mgr, broker, calls = _fake_manager(tmp_path, fake)
    order, intent = _entry_order_and_intent()
    with pytest.raises(TimeoutError):
        _submit_paper_via_manager(order, intent, mgr, broker)
    assert mgr.is_blocked("NVDA", account="paper")
    order2 = dict(order)
    intent2 = _live_intent(order2, {
        "symbol": "NVDA", "direction": "mild_up", "decision_score": 15.5,
        "score": 15.0, "trend_quality": 4.5, "last_price": 167.2,
        "target_notional": 603.36, "intended_notional": 501.56,
    }, "2026-11-26")
    assert intent2["intent_id"] != intent["intent_id"], "a different decision is a different intent"
    assert _submit_paper_via_manager(order2, intent2, mgr, broker) is None
    assert len(calls) == 1, "blocked intent never re-calls the broker"


def test_alpaca_lookup_by_client_order_id_sdk(monkeypatch):
    from broker_alpaca import AccountConfig, AlpacaLeg

    monkeypatch.setenv("ALPACA_PAPER_KEY", "dummy")
    monkeypatch.setenv("ALPACA_PAPER_SECRET", "dummy")
    leg = AlpacaLeg(AccountConfig("paper", 100_000.0, 0.10, True), 0.25, {})
    leg.client = SimpleNamespace(
        get_order_by_client_order_id=lambda cid: SimpleNamespace(
            id="ord-1", symbol="NVDA", status="filled",
            filled_qty="3", qty="3", limit_price="167.18",
            filled_avg_price="167.20"))
    # B17.6: the lookup returns limit_price + filled_avg_price too.
    assert leg.get_by_client_order_id("x") == {
        "id": "ord-1", "symbol": "NVDA", "status": "filled", "filled_qty": 3.0,
        "limit_price": 167.18, "filled_avg_price": 167.20}


def test_alpaca_lookup_by_client_order_id_404_returns_none(monkeypatch):
    from broker_alpaca import AccountConfig, AlpacaLeg

    monkeypatch.setenv("ALPACA_PAPER_KEY", "dummy")
    monkeypatch.setenv("ALPACA_PAPER_SECRET", "dummy")

    class NotFound(Exception):
        pass

    def boom(cid):
        raise NotFound("404: order not found")

    leg = AlpacaLeg(AccountConfig("paper", 100_000.0, 0.10, True), 0.25, {})
    leg.client = SimpleNamespace(get_order_by_client_order_id=boom)
    assert leg.get_by_client_order_id("missing") is None


def test_alpaca_lookup_by_client_order_id_rest_fallback(monkeypatch):
    from broker_alpaca import AccountConfig, AlpacaLeg

    monkeypatch.setenv("ALPACA_PAPER_KEY", "dummy")
    monkeypatch.setenv("ALPACA_PAPER_SECRET", "dummy")
    leg = AlpacaLeg(AccountConfig("paper", 100_000.0, 0.10, True), 0.25, {})
    leg.client = None
    leg._rest_json = lambda method, path: {
        "id": "ord-9", "symbol": "AAPL", "status": "new", "filled_qty": "0",
        "limit_price": None, "filled_avg_price": None}
    # B17.6: the lookup returns limit_price + filled_avg_price too
    # (None here — a market order has no limit, and an unfilled order
    # has no filled average; None means "no price", never 0.0).
    assert leg.get_by_client_order_id("cid-9") == {
        "id": "ord-9", "symbol": "AAPL", "status": "new", "filled_qty": 0.0,
        "limit_price": None, "filled_avg_price": None}


def test_alpaca_lookup_rest_404_returns_none(monkeypatch):
    from broker_alpaca import AccountConfig, AlpacaLeg

    monkeypatch.setenv("ALPACA_PAPER_KEY", "dummy")
    monkeypatch.setenv("ALPACA_PAPER_SECRET", "dummy")
    leg = AlpacaLeg(AccountConfig("paper", 100_000.0, 0.10, True), 0.25, {})
    leg.client = None

    def rest_404(method, path):
        raise RuntimeError("HTTP 404 Not Found")

    leg._rest_json = rest_404
    assert leg.get_by_client_order_id("nope") is None
