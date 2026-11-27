"""
fake_dual_broker.py — test-only DualBroker stand-in for B13 soak readiness.

NOT a backtest, NOT a paper-trading run, NOT connected to Alpaca.
It lets `main.py --broker fake` drive the REAL live-loop code
(strategy_runner.run_daily, non-dry-run branch) against:

- dated fixture bars (bars.json: per-symbol OHLCV DataFrames),
- fixture quotes (quotes.json: bid/ask offsets from the last close;
  the feed timestamp is generated at call time, so quotes are fresh),
- a FILE-BACKED FakeBroker (broker_state.json) whose orders, submit
  call counts, and lookup-visibility counters SURVIVE across
  subprocess restarts — the in-memory FakeBroker alone could not do
  the B13 restart test, because a fresh process would forget the
  accepted order entirely.

The paper leg submits to the persistent fake broker; the live leg is
disabled (mirroring production with ALLOW_LIVE=0: its submit is
called by the loop but refuses and records the call — it never
places an order). No network, no .env keys, no Alpaca client.

Fixture dir layout (built by the test, read here):
  bars.json           {symbol: {"index": [...], "open": [...], "high": [...],
                       "low": [...], "close": [...], "volume": [...]}}
  quotes.json         {symbol: {"bid_offset": float, "ask_offset": float}}
  broker_config.json  {"paper_equity": float, "drop_next_response": bool,
                       "delayed_visibility_polls": int}
  broker_state.json   created/updated by this module (orders, counters,
                      drop flag). A run MUST NOT delete it between the
                      fault-injection runs — it is the broker's memory.

Env: ATS_FAKE_BROKER_DIR = the fixture dir (required with --broker fake).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pandas as pd

from fake_broker import FakeBroker


def _load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return dict(default)
    try:
        data = json.loads(path.read_text())
    except Exception:
        return dict(default)
    return data if isinstance(data, dict) else dict(default)


class FakeDataClient:
    """Bars + quotes from the fixture dir. Read-only after load, so the
    universe screen may call it from worker threads."""

    def __init__(self, fixture_dir: str | Path):
        self.fixture_dir = Path(fixture_dir)
        raw = _load_json(self.fixture_dir / "bars.json", {})
        self._frames: dict[str, pd.DataFrame] = {}
        for symbol, cols in raw.items():
            frame = pd.DataFrame(
                {
                    "open": cols["open"],
                    "high": cols["high"],
                    "low": cols["low"],
                    "close": cols["close"],
                    "volume": cols["volume"],
                },
                index=pd.to_datetime(cols["index"]),
            ).sort_index()
            self._frames[symbol] = frame
        self._quotes = _load_json(self.fixture_dir / "quotes.json", {})

    def get_stock_bars(self, req):
        symbol = getattr(req, "symbol_or_symbols", None)
        if isinstance(symbol, (list, tuple)):
            symbol = symbol[0] if symbol else None
        if symbol not in self._frames:
            # Unknown symbol: the screen treats this as a skip (B5
            # per-symbol isolation), never as fatal.
            raise ValueError(f"no fixture bars for {symbol}")
        return self._frames[symbol]

    def get_stock_latest_quote(self, req):
        symbol = getattr(req, "symbol_or_symbols", None)
        if isinstance(symbol, (list, tuple)):
            symbol = symbol[0] if symbol else None
        if symbol not in self._frames:
            raise ValueError(f"no fixture quote for {symbol}")
        last = float(self._frames[symbol]["close"].iloc[-1])
        offsets = self._quotes.get(symbol, {"bid_offset": -0.01, "ask_offset": 0.01})
        now = datetime.now(timezone.utc).isoformat()
        return {
            symbol: SimpleNamespace(
                bid_price=last + float(offsets.get("bid_offset", -0.01)),
                ask_price=last + float(offsets.get("ask_offset", 0.01)),
                timestamp=now,
            )
        }

    def last_close(self, symbol: str) -> Optional[float]:
        frame = self._frames.get(symbol)
        return float(frame["close"].iloc[-1]) if frame is not None and not frame.empty else None


class FileBackedFakeBroker:
    """FakeBroker whose orders, sequence, lookup-visibility counters,
    drop flag, and call counters persist in broker_state.json.

    The drop flag is loaded from the STATE file when it exists (a
    consumed drop stays consumed across restarts); only a fresh state
    file takes the flag from broker_config.json. delayed_visibility_polls
    comes from the config every time — it is a property of the scenario,
    not of the broker's memory.
    """

    def __init__(self, fixture_dir: str | Path):
        self.fixture_dir = Path(fixture_dir)
        self.state_path = self.fixture_dir / "broker_state.json"
        config = _load_json(self.fixture_dir / "broker_config.json", {})
        self.delayed_visibility_polls = int(config.get("delayed_visibility_polls", 0))
        self.paper_equity = float(config.get("paper_equity", 100000.0))
        self._broker = FakeBroker(
            delayed_visibility_polls=self.delayed_visibility_polls,
            drop_next_response=bool(config.get("drop_next_response", False)),
        )
        self.submit_calls = 0
        self.live_submit_calls = 0
        self.trail_calls: list[dict] = []
        self._load()

    # -- persistence ----------------------------------------------------
    def _load(self) -> None:
        state = _load_json(self.state_path, {})
        if not state:
            return
        broker = self._broker
        broker._seq = int(state.get("seq", 0))
        broker._orders = {k: dict(v) for k, v in state.get("orders", {}).items()}
        broker._by_id = {o["id"]: o for o in broker._orders.values()}
        broker._visibility_counts = {
            k: int(v) for k, v in state.get("visibility_counts", {}).items()
        }
        broker._fills = {}
        # B16 correction: trailing stops live in their own store
        # (they are real broker orders, but not strategy submissions).
        broker._stops = {k: dict(v) for k, v in state.get("stops", {}).items()}
        broker._trail_seq = int(state.get("trail_seq", 0))
        broker._fill_during_cancel = {
            k: (float(v[0]), float(v[1]))
            for k, v in state.get("fill_during_cancel", {}).items()
        }
        broker.fail_next_submit = bool(state.get("fail_next_submit", False))
        # The persisted drop flag wins: a drop consumed by an earlier
        # process must not re-arm on restart.
        broker.drop_next_response = bool(state.get("drop_next_response", False))
        self.submit_calls = int(state.get("submit_calls", 0))
        self.live_submit_calls = int(state.get("live_submit_calls", 0))
        self.trail_calls = list(state.get("trail_calls", []))

    def _save(self) -> None:
        broker = self._broker
        state = {
            "seq": broker._seq,
            "orders": {k: dict(v) for k, v in broker._orders.items()},
            "visibility_counts": dict(broker._visibility_counts),
            "drop_next_response": bool(broker.drop_next_response),
            "submit_calls": self.submit_calls,
            "live_submit_calls": self.live_submit_calls,
            "trail_calls": list(self.trail_calls),
            "stops": {k: dict(v) for k, v in broker._stops.items()},
            "trail_seq": broker._trail_seq,
            "fill_during_cancel": {
                k: [v[0], v[1]] for k, v in broker._fill_during_cancel.items()
            },
            "fail_next_submit": bool(broker.fail_next_submit),
        }
        self.state_path.write_text(json.dumps(state, indent=2))

    def save(self) -> None:
        self._save()

    # -- broker interface (paper leg) ------------------------------------
    def submit(self, order: dict) -> dict:
        self.submit_calls += 1
        try:
            return self._broker.submit(order)
        finally:
            # Persist even when submit raises (accept + drop): the
            # order IS stored in the broker, and the drop flag is now
            # consumed — both must survive the restart.
            self._save()

    def get_by_client_order_id(self, client_order_id: str) -> Optional[dict]:
        try:
            return self._broker.get_by_client_order_id(client_order_id)
        finally:
            # The visibility counter advanced; persist it so the
            # next process resumes the count, not the scenario.
            self._save()

    def fill(self, client_order_id: str, fill_id: str, qty: float, price: float) -> dict:
        """Apply a (partial) fill and persist it, so a later process
        sees the filled quantity (B15)."""
        try:
            return self._broker.apply_partial_fill(client_order_id, fill_id, qty, price)
        finally:
            self._save()

    def cancel(self, client_order_id: str) -> dict:
        """Cancel an open order and persist it (B15): the unfilled
        remainder stops working; filled shares stay filled."""
        try:
            return self._broker.cancel(client_order_id)
        finally:
            self._save()

    def expire(self, client_order_id: str) -> dict:
        """Expire an open order and persist it (B16): a DAY order that
        reached the close unfilled. Filled shares stay filled."""
        try:
            return self._broker.expire(client_order_id)
        finally:
            self._save()

    def done_for_day(self, client_order_id: str) -> dict:
        """Mark an order done_for_day and persist it (B16 correction):
        a DAY order that reached the close partially filled. Filled
        shares stay filled; the remainder is still working and can
        fill the next trading day."""
        try:
            return self._broker.done_for_day(client_order_id)
        finally:
            self._save()

    def set_fill_during_cancel(self, client_order_id: str, qty: float, price: float) -> None:
        """Arm a fill that lands between the cancel request and the
        broker's confirmation (test 2), and persist the arm so the
        next process's cancel applies it."""
        self._broker.set_fill_during_cancel(client_order_id, qty, price)
        self._save()

    def fail_next_submit(self) -> None:
        """Arm a genuine submit failure (test 3c) and persist the arm
        so the next process's submit raises before storing anything."""
        self._broker.fail_next_submit = True
        self._save()

    def attach_trailing_stop(self, symbol: str, qty: float, trail_pct: float) -> dict:
        """Attach a trailing-stop SELL (B16 correction) and persist
        it: a real broker order in its own store, not a strategy
        submission."""
        try:
            return self._broker.attach_trailing_stop(symbol, qty, trail_pct)
        finally:
            self._save()

    def trigger_trailing_stop(self, client_order_id: str, price: float) -> dict:
        """Elect a trailing stop and persist the fill (test 3): the
        stop sells, capped at the shares actually held."""
        try:
            return self._broker.trigger_trailing_stop(client_order_id, price)
        finally:
            self._save()

    def list_stops(self) -> list[dict]:
        return self._broker.list_stops()

    def order_count(self) -> int:
        return self._broker.order_count()

    def list_orders(self) -> list[dict]:
        return self._broker.list_orders()


class FakeLeg:
    """One account leg. The paper leg trades on the persistent fake
    broker; the live leg is disabled (ALLOW_LIVE=0 posture): submit is
    called by the loop but refuses and only records the call."""

    def __init__(self, mode: str, store: FileBackedFakeBroker, data_client: FakeDataClient):
        self.mode = mode
        self._store = store
        self.data = data_client if mode == "paper" else None
        self.client = None  # no Alpaca client: universe falls back to seed list
        self.data_clock = None
        self.enabled = mode == "paper"

    # -- account state ----------------------------------------------------
    def equity(self) -> Optional[float]:
        if self.mode == "paper":
            return self._store.paper_equity
        return 0.0

    def positions(self) -> dict:
        if self.mode != "paper":
            return {}
        # Filled holdings ONLY, like the real Alpaca leg (GET
        # /v2/positions): an accepted-but-unfilled order is NOT a
        # position. The unfilled part of an open order is visible via
        # open_orders() and reconcile() counts it there — so the
        # strategy's expected position (recorded on submit) is not
        # flagged as drift while the order is still working.
        # B15: a cancelled order's FILLED shares are still a holding
        # (real Alpaca reports them in /v2/positions) — only rejected
        # orders (never filled by construction) are skipped.
        # B16 correction: a trailing stop's fills are holdings too
        # (negative — the stop sold shares), so stops are netted here
        # alongside strategy orders.
        net: dict[str, float] = {}
        for order in self._store.list_orders() + self._store.list_stops():
            if str(order.get("status", "")).lower() in {"rejected"}:
                continue
            filled = float(order.get("filled_qty") or 0.0)
            if filled == 0:
                continue
            signed = filled if order.get("side") == "buy" else -filled
            net[order["symbol"]] = net.get(order["symbol"], 0.0) + signed
        out = {}
        for symbol, qty in net.items():
            if qty == 0:
                continue
            last = self.data.last_close(symbol) if self.data is not None else None
            price = last if last is not None else 0.0
            out[symbol] = {
                "qty": qty,
                "notional": abs(qty) * price,
                "avg_entry": price,
                "unrealized_pl": 0.0,
            }
        return out

    def open_orders(self) -> list[dict]:
        """Open orders for this leg (the unfilled part of each). The
        fake broker never fills on its own, so every submitted order
        is open until cancelled/rejected or filled via fill()."""
        if self.mode != "paper":
            return []
        out = []
        # B16 correction: working trailing stops are open orders too
        # (the strategy's exit must be able to see and cancel them),
        # so they are merged in here — with their order_type, which
        # is how the strategy tells a stop from a strategy order.
        for order in self._store.list_orders() + self._store.list_stops():
            # B16: expired / done_for_day orders are excluded, like
            # Alpaca's QueryOrderStatus.OPEN — an expired order's
            # remaining qty must not be counted as an open position
            # once the state sync clears the expected position to 0.
            if str(order.get("status", "")).lower() in {
                    "cancelled", "canceled", "rejected", "filled",
                    "expired", "done_for_day"}:
                continue
            out.append({
                "id": str(order.get("id")),
                "client_order_id": str(order.get("client_order_id")),
                "symbol": str(order.get("symbol")),
                "side": str(order.get("side", "")).lower(),
                "qty": float(order.get("qty") or 0.0),
                "filled_qty": float(order.get("filled_qty") or 0.0),
                "status": str(order.get("status", "")).lower(),
                "order_type": str(order.get("order_type", "")).lower(),
                # B15: pass the stored prices through (limit_price was
                # stored at submit; filled_avg_price is maintained by
                # apply_partial_fill). Either may be None — the sizing
                # reservation treats None as "no price", never as 0.0.
                "limit_price": (float(order["limit_price"])
                                if order.get("limit_price") is not None else None),
                "filled_avg_price": (float(order["filled_avg_price"])
                                     if order.get("filled_avg_price") is not None else None),
            })
        return out

    def is_market_open(self) -> Optional[bool]:
        return True

    def floor_tripped(self) -> bool:
        return False

    def can_trade(self) -> bool:
        return self.enabled

    # -- market data --------------------------------------------------------
    def latest_quote(self, symbol: str) -> Optional[dict]:
        if self.data is None:
            return None
        try:
            quotes = self.data.get_stock_latest_quote(
                SimpleNamespace(symbol_or_symbols=symbol)
            )
        except Exception:
            return None
        quote = quotes[symbol]
        out = {
            "bid": float(quote.bid_price),
            "ask": float(quote.ask_price),
            "spread": float(quote.ask_price) - float(quote.bid_price),
            "timestamp": quote.timestamp,
        }
        clock = getattr(self, "data_clock", None)
        if clock is not None:
            clock.note(out["timestamp"])
        return out

    # -- orders ---------------------------------------------------------------
    def submit(self, order: dict) -> Optional[dict]:
        if not self.can_trade():
            # Disabled leg: refuse without placing anything. The call
            # is still recorded — the test asserts the live leg was
            # invoked (the loop calls it, as in production) but placed
            # zero orders.
            self._store.live_submit_calls += 1
            self._store.save()
            return None
        return self._store.submit(order)

    def get_by_client_order_id(self, client_order_id: str) -> Optional[dict]:
        if self.mode != "paper":
            return None
        return self._store.get_by_client_order_id(client_order_id)

    def cancel_order(self, order_id=None, client_order_id=None) -> Optional[dict]:
        """Cancel one open order (B16), in the open-orders shape.

        Returns the cancelled order, or None when the leg has no such
        order (disabled leg, or already gone — success: nothing
        remains to fill). The caller confirms via an open-orders
        re-fetch, so a None here is never the sole confirmation.
        Trailing stops are cancellable the same way (by id or
        client_order_id) — the exit cancels the stop before selling.
        """
        if self.mode != "paper":
            return None
        target = None
        if client_order_id is not None:
            target = self._store.get_by_client_order_id(client_order_id)
        elif order_id is not None:
            for candidate in self._store.list_orders() + self._store.list_stops():
                if str(candidate.get("id")) == str(order_id):
                    target = candidate
                    break
        if target is None:
            return None
        cancelled = self._store.cancel(target["client_order_id"])
        return {
            "id": str(cancelled.get("id")),
            "client_order_id": str(cancelled.get("client_order_id")),
            "symbol": str(cancelled.get("symbol")),
            "side": str(cancelled.get("side", "")).lower(),
            "qty": float(cancelled.get("qty") or 0.0),
            "filled_qty": float(cancelled.get("filled_qty") or 0.0),
            "status": str(cancelled.get("status", "")).lower(),
            "order_type": str(cancelled.get("order_type", "")).lower(),
            "limit_price": (float(cancelled["limit_price"])
                            if cancelled.get("limit_price") is not None else None),
            "filled_avg_price": (float(cancelled["filled_avg_price"])
                                 if cancelled.get("filled_avg_price") is not None else None),
        }

    def attach_trailing_stop(self, symbol: str, qty: float, trail_pct: float) -> Optional[dict]:
        """Attach a trailing-stop SELL (B16 correction): a REAL broker
        order in the fake broker's stop store — not just a recorded
        call — so the strategy's exit can cancel it, reconcile can
        see its fills, and a test can elect it. The trail_calls list
        is kept as the attach audit trail (B13 asserts its length).
        A disabled leg (live) attaches nothing, like the real leg."""
        if not self.enabled:
            return None
        stop = self._store.attach_trailing_stop(symbol, qty, trail_pct)
        self._store.trail_calls.append(
            {"symbol": symbol, "qty": float(qty), "trail_pct": float(trail_pct)}
        )
        self._store.save()
        return {
            "id": str(stop["id"]),
            "client_order_id": str(stop["client_order_id"]),
            "symbol": symbol,
            "trail_pct": trail_pct,
        }


class FakeDualBroker:
    """DualBroker stand-in: paper + live legs, one shared clock slot,
    market always open (the fixture IS a trading day)."""

    def __init__(self, fixture_dir: str | Path):
        fixture_dir = Path(fixture_dir)
        if not (fixture_dir / "bars.json").exists():
            raise SystemExit(
                f"ATS_FAKE_BROKER_DIR {fixture_dir} has no bars.json; "
                "refusing to run the fake broker without fixtures"
            )
        self.fixture_dir = fixture_dir
        self._store = FileBackedFakeBroker(fixture_dir)
        self._data = FakeDataClient(fixture_dir)
        self.paper = FakeLeg("paper", self._store, self._data)
        self.live = FakeLeg("live", self._store, self._data)
        self.data_clock = None
        self.state: dict = {}

    def is_market_open(self) -> bool:
        return True

    def save_state(self) -> None:
        self._store.save()

    def status(self) -> dict:
        return {
            "paper": {
                "equity": self.paper.equity(),
                "can_trade": self.paper.can_trade(),
                "market_open": True,
                "positions": len(self.paper.positions()),
            },
            "live": {
                "equity": self.live.equity(),
                "can_trade": self.live.can_trade(),
                "market_open": True,
                "floor_tripped": self.live.floor_tripped(),
                "positions": len(self.live.positions()),
            },
        }
