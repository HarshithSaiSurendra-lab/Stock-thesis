"""B15 — sizing reservations for open buy orders.

The gap (B13 fallout): `run_daily` sized new buys from FILLED
positions only, so an open buy order's unfilled quantity was treated
as free capital. A second buy could then be sized as if the first
order's money were still available. B13 already made reconcile()
count open orders when checking positions; sizing is the symmetric
fix: an open buy reserves remaining * reservation price against the
deployed budget.

Price chain for the reservation (never invented — see
`_reservation_price_for_order`): limit_price, then filled_avg_price,
then the symbol's current last price from this run's candidates, then
the entry price recorded in the strategy state when the order was
submitted. Sells never reserve buy budget.

E2E shape (through main.py as a SUBPROCESS, fake broker, dated
fixtures — same harness as B13): the cap is max_deployed = 1000
(STRATEGY_CAPITAL 2500 x MAX_DEPLOYED_PCT 0.4). Run 1 buys NVDA
alone (AMD flat, not a candidate): qty 2 @ limit ~167.25, notional
~334.54, reservation ~334.50. Run 2 sizes AMD (uptrend bars,
last ~71.61, vol_scale ~0.962) against the remaining ~665.50.

Everything offline and deterministic: Alpaca keys scrubbed, cwd is a
temp dir, ALLOW_LIVE=0, live leg never places an order.
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
    _frame, _guard_timing, _latest_decision_log, _sessions,
)
from fake_dual_broker import FileBackedFakeBroker  # noqa: E402
from strategy_runner import _open_buy_reservations, _reservation_price_for_order  # noqa: E402

VENV_PY = REPO / ".venv" / "bin" / "python"

# Cap for every e2e run: strategy capital 2500 x 0.4 = 1000 deployed.
MAX_DEPLOYED = 1000.0
N_SESSIONS = 330


# ---------------------------------------------------------------- fixtures

def _write_fixture(fixture_dir: Path, *, nvda_cycle=(0.016, 0.016, -0.016),
                   amd_cycle=(0.0,), amd_start=30.0,
                   paper_equity: float = 100_000.0) -> dict:
    """Write bars.json / quotes.json / broker_config.json. Never touches
    broker_state.json — the fake broker's memory persists across runs.
    AMD is the second symbol (it is in the seed list; XLK is only a
    sector benchmark and is never a candidate)."""
    fixture_dir.mkdir(parents=True, exist_ok=True)
    sessions = _sessions()
    frames = {
        "SPY": _frame(400.0, (0.0012,), 80_000_000, sessions),
        "NVDA": _frame(30.0, nvda_cycle, 5_000_000, sessions),
        "XLK": _frame(150.0, (0.008, -0.008), 20_000_000, sessions),
        "AMD": _frame(amd_start, amd_cycle, 8_000_000, sessions),
    }
    (fixture_dir / "bars.json").write_text(json.dumps(frames))
    quotes = {}
    for sym, frame in frames.items():
        last = frame["close"][-1]
        quotes[sym] = {"bid_offset": -0.01, "ask_offset": 0.01, "last": last}
    (fixture_dir / "quotes.json").write_text(json.dumps(quotes))
    (fixture_dir / "broker_config.json").write_text(json.dumps({
        "paper_equity": paper_equity,
        "drop_next_response": False,
        "delayed_visibility_polls": 0,
    }))
    return {sym: frame["close"][-1] for sym, frame in frames.items()}


AMD_UPTREND = (0.008, 0.008, -0.008)  # last ~71.61 from a 30.0 start
NVDA_FALLING = (-0.016, -0.016, 0.016)

_SCRUB = (
    "ALPACA_PAPER_KEY", "ALPACA_PAPER_SECRET", "ALPACA_LIVE_KEY", "ALPACA_LIVE_SECRET",
    "NOTIFIER_TOKEN", "NOTIFIER_CHAT_ID", "ORDER_STORE_PATH", "STRATEGY_STATE_PATH",
    "GUARDIAN_STATE_PATH", "BROKER_STATE_PATH", "MEMORY_DB_PATH",
    "EXECUTION_JOURNAL_DB_PATH", "ALERT_LOG_PATH", "KILL_SWITCH_PATH",
    "DECISION_LOG_DIR", "BAR_CACHE_DIR", "BAR_CACHE_ENABLED",
    "EXIT_ON_SIGNAL_LOSS", "UNIVERSE_SEED_SYMBOLS",
    "MAX_DEPLOYED_PCT", "MAX_POSITION_PCT", "TARGET_N_POSITIONS", "STRATEGY_CAPITAL",
)


def _env(home: Path, fixture_dir: Path, *, exit_on_signal_loss: int = 0,
         max_deployed_pct: float = 0.4) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
    env.update({
        "ATS_FAKE_BROKER_DIR": str(fixture_dir),
        "ALLOW_LIVE": "0",
        "BAR_CACHE_ENABLED": "0",
        "EXIT_ON_SIGNAL_LOSS": str(exit_on_signal_loss),
        "MAX_DEPLOYED_PCT": str(max_deployed_pct),
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
              exit_on_signal_loss: int = 0,
              max_deployed_pct: float = 0.4) -> tuple[int, str]:
    proc = subprocess.run(
        [str(VENV_PY), str(REPO / "main.py"), *args],
        cwd=str(home),
        env=_env(home, fixture_dir, exit_on_signal_loss=exit_on_signal_loss,
                 max_deployed_pct=max_deployed_pct),
        capture_output=True, text=True, timeout=300,
    )
    return proc.returncode, proc.stdout + proc.stderr


def _broker_state(fixture_dir: Path) -> dict:
    return json.loads((fixture_dir / "broker_state.json").read_text())


def _open_order_coid(fixture_dir: Path, symbol: str) -> str:
    """client_order_id of the (single) open order for a symbol."""
    orders = _broker_state(fixture_dir)["orders"]
    open_coids = [coid for coid, o in orders.items()
                  if o.get("symbol") == symbol
                  and str(o.get("status", "")).lower() not in {
                      "cancelled", "canceled", "rejected", "filled"}]
    assert len(open_coids) == 1, orders
    return open_coids[0]


def _order_for(log: dict, symbol: str) -> dict:
    matches = [o for o in log.get("orders", []) if o["symbol"] == symbol]
    assert len(matches) == 1, log.get("orders")
    return matches[0]


def _nvda_limit(last_close: float) -> float:
    """The limit a mild_up buy gets: bid = last close - 0.01, rounded."""
    return round(last_close - 0.01, 2)


# ---------------------------------------------------------------- unit tests:
# the reservation helper and its price chain (no broker, no network)

def _buy(symbol="NVDA", qty=3.0, filled=0.0, **extra):
    order = {"id": "fake-0001", "symbol": symbol, "side": "buy",
             "qty": qty, "filled_qty": filled, "status": "submitted"}
    order.update(extra)
    return order


def test_price_chain_limit_wins():
    order = _buy(limit_price=150.0, filled_avg_price=140.0)
    price, source = _reservation_price_for_order(
        order, {"NVDA": 130.0}, {"NVDA": 120.0})
    assert (price, source) == (150.0, "limit_price")


def test_price_chain_filled_avg_for_market_order():
    # A market buy has no limit; the average its fills actually got is
    # the best estimate for the rest.
    order = _buy(limit_price=None, filled_avg_price=140.0)
    price, source = _reservation_price_for_order(
        order, {"NVDA": 130.0}, {"NVDA": 120.0})
    assert (price, source) == (140.0, "filled_avg_price")


def test_price_chain_last_price_for_unfilled_market_order():
    order = _buy(limit_price=None, filled_avg_price=None)
    price, source = _reservation_price_for_order(
        order, {"NVDA": 130.0}, {"NVDA": 120.0})
    assert (price, source) == (130.0, "last_price")


def test_price_chain_entry_price_when_not_a_candidate():
    # The normal case for an open order: the symbol was submitted today
    # and is deduped out of this run's candidates, so no last price —
    # the entry price recorded at submit time is the fallback.
    order = _buy(limit_price=None, filled_avg_price=None)
    price, source = _reservation_price_for_order(order, {}, {"NVDA": 120.0})
    assert (price, source) == (120.0, "entry_price")


def test_price_chain_none_reserves_nothing():
    order = _buy(limit_price=None, filled_avg_price=None)
    assert _reservation_price_for_order(order, {}, {}) == (None, None)
    total, detail = _open_buy_reservations([order], {"NVDA"}, {}, {})
    assert (total, detail) == (0.0, [])


def test_reservations_partial_fill_counts_remaining_only():
    # qty 10, filled 4 @ limit 50: the filled 4 are a position (counted
    # in paper_positions), the reservation is the remaining 6 only.
    total, detail = _open_buy_reservations(
        [_buy(qty=10.0, filled=4.0, limit_price=50.0)], {"NVDA"}, {}, {})
    assert total == 300.0
    assert detail[0]["remaining_qty"] == 6.0
    assert detail[0]["price_source"] == "limit_price"


def test_reservations_sell_never_reserves():
    order = {"id": "fake-0002", "symbol": "NVDA", "side": "sell",
             "qty": 3.0, "filled_qty": 0.0, "status": "submitted",
             "limit_price": 150.0}
    total, detail = _open_buy_reservations([order], {"NVDA"}, {}, {})
    assert (total, detail) == (0.0, [])


def test_reservations_non_strategy_symbol_ignored():
    total, detail = _open_buy_reservations(
        [_buy(symbol="AAPL", limit_price=150.0)], {"NVDA"}, {}, {})
    assert (total, detail) == (0.0, [])


def test_reservations_fully_filled_order_reserves_nothing():
    total, detail = _open_buy_reservations(
        [_buy(qty=3.0, filled=3.0, limit_price=150.0)], {"NVDA"}, {}, {})
    assert (total, detail) == (0.0, [])


def test_reservations_multiple_orders_accumulate():
    orders = [_buy(qty=3.0, limit_price=100.0),
              {"id": "fake-0002", "symbol": "NVDA", "side": "buy",
               "qty": 2.0, "filled_qty": 1.0, "status": "partially_filled",
               "limit_price": 110.0}]
    total, detail = _open_buy_reservations(orders, {"NVDA"}, {}, {})
    assert total == 3.0 * 100.0 + 1.0 * 110.0
    assert len(detail) == 2


# ---------------------------------------------------------------- unit tests:
# AlpacaLeg.open_orders carries the price fields (SDK + REST)

def test_alpaca_open_orders_sdk_carries_prices(monkeypatch):
    from broker_alpaca import AccountConfig, AlpacaLeg

    monkeypatch.setenv("ALPACA_PAPER_KEY", "dummy")
    monkeypatch.setenv("ALPACA_PAPER_SECRET", "dummy")
    leg = AlpacaLeg(AccountConfig("paper", 100_000.0, 0.10, True), 0.25, {})
    leg.client = SimpleNamespace(
        get_orders=lambda req: [
            SimpleNamespace(id="ord-1", symbol="NVDA", side="buy", qty="3",
                            filled_qty="0", status="new",
                            limit_price="167.25", filled_avg_price=None),
            SimpleNamespace(id="ord-2", symbol="AMD", side="buy", qty="5",
                            filled_qty="2", status="partially_filled",
                            limit_price=None, filled_avg_price="71.50"),
        ])
    orders = leg.open_orders()
    assert orders[0]["limit_price"] == 167.25
    assert orders[0]["filled_avg_price"] is None
    assert orders[1]["limit_price"] is None
    assert orders[1]["filled_avg_price"] == 71.50


def test_alpaca_open_orders_rest_carries_prices(monkeypatch):
    from broker_alpaca import AccountConfig, AlpacaLeg

    monkeypatch.setenv("ALPACA_PAPER_KEY", "dummy")
    monkeypatch.setenv("ALPACA_PAPER_SECRET", "dummy")
    leg = AlpacaLeg(AccountConfig("paper", 100_000.0, 0.10, True), 0.25, {})
    leg.client = None
    leg._rest_json = lambda method, path: [
        {"id": "ord-1", "symbol": "NVDA", "side": "buy", "qty": "3",
         "filled_qty": "0", "status": "new", "limit_price": "167.25",
         "filled_avg_price": None}]
    orders = leg.open_orders()
    assert orders[0]["limit_price"] == 167.25
    assert orders[0]["filled_avg_price"] is None


# ---------------------------------------------------------------- e2e

def test_e2e_two_buys_share_one_cap(tmp_path):
    """Test 1: two buys that each fit individually but exceed the cap
    together. Run 1 buys NVDA alone (reservation ~334.50). Run 2 sizes
    AMD against the remaining ~665.50 — sized down to 8 shares
    (572.88), so reservation + AMD <= 1000. A control run with no open
    order sizes the same AMD at 13 shares (930.93), which together
    with the NVDA reservation would exceed the cap (1265.43)."""
    _guard_timing()
    home = tmp_path / "home"
    fixture = tmp_path / "fixture"
    home.mkdir(parents=True)
    lasts = _write_fixture(fixture)  # AMD flat: not a candidate
    nvda_last = lasts["NVDA"]
    limit = _nvda_limit(nvda_last)

    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"])
    assert rc == 0, out
    log1 = _latest_decision_log(home)
    assert log1["status"] == "ok", out
    nvda = _order_for(log1, "NVDA")
    assert nvda["order"]["qty"] == 2 and nvda["order"]["limit_price"] == limit
    # intended uses the ask (last close + 0.01), not the close.
    assert nvda["intended_notional"] == round(2 * (nvda_last + 0.01), 2)
    reservation = round(2 * limit, 2)

    _write_fixture(fixture, amd_cycle=AMD_UPTREND)  # AMD signals now
    rc, out = _run_main(home, fixture, ["--dry-run", "--mode", "regular",
                                        "--broker", "fake"])
    assert rc == 0, out
    log2 = _latest_decision_log(home)
    assert log2["status"] == "dry_run", out
    budget = log2["budget"]
    assert budget["max_deployed"] == MAX_DEPLOYED
    assert budget["existing_notional"] == reservation, budget
    assert budget["remaining_budget"] == round(MAX_DEPLOYED - reservation, 2)
    assert budget["open_buy_reservations"] == reservation
    assert budget["buy_reservations"] == [{
        "symbol": "NVDA", "order_id": "fake-0001", "remaining_qty": 2.0,
        "price": limit, "price_source": "limit_price", "notional": reservation}]
    amd = _order_for(log2, "AMD")
    assert amd["order"]["qty"] == 8, amd
    assert reservation + amd["intended_notional"] <= MAX_DEPLOYED

    # Control: same AMD bars, same cap, no open order — AMD is bigger.
    home_c = tmp_path / "home_control"
    fixture_c = tmp_path / "fixture_control"
    home_c.mkdir(parents=True)
    _write_fixture(fixture_c, nvda_cycle=(0.0,), amd_cycle=AMD_UPTREND)
    rc, out = _run_main(home_c, fixture_c, ["--dry-run", "--mode", "regular",
                                            "--broker", "fake"])
    assert rc == 0, out
    log_c = _latest_decision_log(home_c)
    amd_c = _order_for(log_c, "AMD")
    assert amd_c["order"]["qty"] == 13, amd_c
    assert amd_c["intended_notional"] > amd["intended_notional"]
    assert reservation + amd_c["intended_notional"] > MAX_DEPLOYED


def test_e2e_partial_fill_counts_once(tmp_path):
    """Test 2: NVDA buy (qty 2) partially fills 1 share. Committed =
    filled position notional (1 x last close) + remaining reservation
    (1 x limit) — the filled share is counted once, not in both."""
    _guard_timing()
    home = tmp_path / "home"
    fixture = tmp_path / "fixture"
    home.mkdir(parents=True)
    lasts = _write_fixture(fixture)
    nvda_last = lasts["NVDA"]
    limit = _nvda_limit(nvda_last)

    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"])
    assert rc == 0, out
    coid = _open_order_coid(fixture, "NVDA")
    FileBackedFakeBroker(fixture).fill(coid, "fill-1", 1, limit)

    _write_fixture(fixture, amd_cycle=AMD_UPTREND)
    rc, out = _run_main(home, fixture, ["--dry-run", "--mode", "regular",
                                        "--broker", "fake"])
    assert rc == 0, out
    log2 = _latest_decision_log(home)
    budget = log2["budget"]
    filled_notional = 1 * nvda_last  # positions value the fill at the last close
    remaining_reservation = round(1 * limit, 2)
    # Exactly once: filled in the position, remaining in the reservation.
    assert budget["existing_notional"] == round(filled_notional + remaining_reservation, 2), budget
    assert budget["existing_notional"] != round(filled_notional + 2 * limit, 2), (
        "double-counted: filled shares also reserved")
    assert budget["buy_reservations"][0]["remaining_qty"] == 1.0
    assert budget["buy_reservations"][0]["price_source"] == "limit_price"
    amd = _order_for(log2, "AMD")
    assert amd["intended_notional"] <= budget["remaining_budget"]


def test_e2e_cancel_releases_unfilled_only(tmp_path):
    """Test 3: NVDA buy (qty 2) partially fills 1 share, then the
    remainder is cancelled. The filled share stays counted, the
    unfilled reservation drops to zero, and AMD can then use the freed
    budget (11 shares vs 8 with the reservation in place)."""
    _guard_timing()
    home = tmp_path / "home"
    fixture = tmp_path / "fixture"
    home.mkdir(parents=True)
    lasts = _write_fixture(fixture)
    nvda_last = lasts["NVDA"]
    limit = _nvda_limit(nvda_last)

    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"])
    assert rc == 0, out
    coid = _open_order_coid(fixture, "NVDA")
    store = FileBackedFakeBroker(fixture)
    store.fill(coid, "fill-1", 1, limit)

    _write_fixture(fixture, amd_cycle=AMD_UPTREND)
    rc, out = _run_main(home, fixture, ["--dry-run", "--mode", "regular",
                                        "--broker", "fake"])
    assert rc == 0, out
    log_a = _latest_decision_log(home)
    filled_notional = 1 * nvda_last  # positions value the fill at the last close
    assert log_a["budget"]["existing_notional"] == round(filled_notional + limit, 2), log_a["budget"]
    amd_a = _order_for(log_a, "AMD")
    assert amd_a["order"]["qty"] == 8, amd_a

    store.cancel(coid)
    rc, out = _run_main(home, fixture, ["--dry-run", "--mode", "regular",
                                        "--broker", "fake"])
    assert rc == 0, out
    log_b = _latest_decision_log(home)
    # Filled share still counted; unfilled reservation gone.
    assert log_b["budget"]["existing_notional"] == round(filled_notional, 2), log_b["budget"]
    assert log_b["budget"]["open_buy_reservations"] == 0.0
    assert log_b["budget"]["buy_reservations"] == []
    amd_b = _order_for(log_b, "AMD")
    assert amd_b["order"]["qty"] == 11, amd_b
    assert amd_b["intended_notional"] > amd_a["intended_notional"]


def test_e2e_open_sell_reserves_nothing(tmp_path):
    """Test 4: the only way an open SELL arises in this system is an
    exit. Buy NVDA, exit it on a falling fixture (sell submitted,
    open, unfilled), cancel the stale buy, then size AMD: the open
    sell must hold no buy budget — existing_notional is 0 and AMD gets
    the full cap (13 shares, same as the no-open-order control)."""
    _guard_timing()
    home = tmp_path / "home"
    fixture = tmp_path / "fixture"
    home.mkdir(parents=True)
    _write_fixture(fixture)

    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"],
                        exit_on_signal_loss=1)
    assert rc == 0, out
    log1 = _latest_decision_log(home)
    assert log1["status"] == "ok" and len(log1["orders"]) == 1, out

    # NVDA falls: signal lost, exit_on_signal_loss exits the entry.
    _write_fixture(fixture, nvda_cycle=NVDA_FALLING)
    rc, out = _run_main(home, fixture, ["--mode", "regular", "--broker", "fake"],
                        exit_on_signal_loss=1)
    assert rc == 0, out
    log2 = _latest_decision_log(home)
    assert log2["status"] == "ok", out
    assert log2["orders"] == [], "no new entry while exiting"
    state = _broker_state(fixture)
    assert len(state["orders"]) == 2, state
    orders = sorted(state["orders"].values(), key=lambda o: o["id"])
    assert orders[0]["side"] == "buy" and orders[1]["side"] == "sell"
    assert orders[1]["qty"] == orders[0]["qty"] == 2.0
    with open(home / "strategy_state.json") as fh:
        assert json.load(fh)["open_entries"] == {}, "entry closed after the exit"

    # Cancel the stale working buy; only the open sell remains.
    FileBackedFakeBroker(fixture).cancel(orders[0]["client_order_id"])
    _write_fixture(fixture, nvda_cycle=NVDA_FALLING, amd_cycle=AMD_UPTREND)
    rc, out = _run_main(home, fixture, ["--dry-run", "--mode", "regular",
                                        "--broker", "fake"])
    assert rc == 0, out
    log3 = _latest_decision_log(home)
    assert log3["budget"]["existing_notional"] == 0.0, log3["budget"]
    assert log3["budget"]["open_buy_reservations"] == 0.0
    amd = _order_for(log3, "AMD")
    assert amd["order"]["qty"] == 13, amd
    assert amd["intended_notional"] <= MAX_DEPLOYED
