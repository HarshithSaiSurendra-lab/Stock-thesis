"""
broker_alpaca.py — Alpaca dual-account adapter.

Runs ONE signal against TWO accounts at once:
  - PAPER: idealized fills, the "what the strategy theoretically earned" baseline.
  - LIVE : real fills with real slippage, the "what I actually captured" reality.
The gap between them is your live measurement of execution cost.

Safety baked in:
  - CAPITAL FLOOR: if the LIVE account equity falls to or below the floor
    (default 80% of its starting capital, i.e. a -20% drawdown), the adapter
    liquidates all live positions and halts the live leg PERMANENTLY. Paper
    keeps running as a benchmark.
  - The live leg never trades after the floor is tripped, even on restart
    (the tripped state is persisted to disk).

Keys are read from environment variables ONLY. Never hardcode them, never paste
them in chat. Set, in your shell or a local .env you do not commit:
    ALPACA_PAPER_KEY / ALPACA_PAPER_SECRET
    ALPACA_LIVE_KEY  / ALPACA_LIVE_SECRET

This module is intentionally thin and auditable. It does NOT decide what to
trade — that's the strategy's job. It executes vetted orders and enforces the
floor.
"""

from __future__ import annotations
import os
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Literal

log = logging.getLogger("broker.alpaca")

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import (
        MarketOrderRequest, LimitOrderRequest, TrailingStopOrderRequest,
    )
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockLatestQuoteRequest, StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    _ALPACA_AVAILABLE = True
except ImportError:
    _ALPACA_AVAILABLE = False


Mode = Literal["paper", "live"]


@dataclass
class AccountConfig:
    mode: Mode
    starting_capital: float
    floor_pct: float = 0.80          # liquidate+halt if equity <= 80% of start
    enabled: bool = True


@dataclass
class BrokerConfig:
    paper: AccountConfig = field(
        default_factory=lambda: AccountConfig("paper", 1000.0, enabled=True)
    )
    live: AccountConfig = field(
        default_factory=lambda: AccountConfig("live", 1000.0, floor_pct=0.80, enabled=True)
    )
    max_position_pct: float = 0.50   # single position cap (of that account's equity)
    state_path: str = "./broker_state.json"


def _normalized_order_options(order: dict) -> dict:
    time_in_force = str(order.get("time_in_force", "day")).lower()
    extended_hours = bool(order.get("extended_hours", False))
    order_type = str(order.get("order_type", "")).lower()
    if extended_hours:
        if order_type != "limit":
            raise ValueError("extended-hours orders must be limit orders")
        if time_in_force not in {"day", "gtc"}:
            raise ValueError("extended-hours orders require day or gtc time_in_force")
    return {"time_in_force": time_in_force, "extended_hours": extended_hours}


def _rest_order_payload(order: dict, options: Optional[dict] = None) -> dict:
    options = options or _normalized_order_options(order)
    payload = {
        "symbol": order["symbol"],
        "qty": str(order["qty"]),
        "side": order["side"],
        "type": order["order_type"],
        "time_in_force": options["time_in_force"],
    }
    if order["order_type"] == "limit":
        payload["limit_price"] = str(order["limit_price"])
    if order.get("client_order_id"):
        # Idempotency key: Alpaca rejects a duplicate client_order_id
        # (409), so a retry after a timeout with the same ID is safe.
        payload["client_order_id"] = str(order["client_order_id"])
    if options["extended_hours"]:
        payload["extended_hours"] = True
    return payload


def _time_in_force_enum(value: str):
    normalized = value.lower()
    if normalized == "gtc":
        return TimeInForce.GTC
    return TimeInForce.DAY


def _opt_float(value) -> Optional[float]:
    """float(value) or None when the broker leaves the field unset.

    Alpaca returns prices as strings, Decimals, or null depending on
    the path (SDK vs REST) and the order type (a market order has no
    limit_price). Callers treat None as "no price", never as 0.0.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _status_str(value) -> str:
    """Broker status as a lowercase string, from an SDK enum or a plain string.

    ``str(OrderStatus.EXPIRED)`` is ``"OrderStatus.EXPIRED"``; the
    REST path returns ``"expired"``. Both normalize to ``"expired"``.
    """
    raw = getattr(value, "value", value)
    text = str(raw or "").lower()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text


class AlpacaLeg:
    """One account (paper or live) wrapped with the capital floor."""

    PAPER_URL = "https://paper-api.alpaca.markets"
    LIVE_URL = "https://api.alpaca.markets"

    def __init__(self, cfg: AccountConfig, max_position_pct: float, state: dict):
        self.cfg = cfg
        self.max_position_pct = max_position_pct
        self.state = state  # shared persisted dict, namespaced by mode below
        self._key_state.setdefault("floor_tripped", False)
        self._key_state.setdefault("starting_capital", cfg.starting_capital)

        if cfg.mode == "paper":
            key = os.environ.get("ALPACA_PAPER_KEY")
            secret = os.environ.get("ALPACA_PAPER_SECRET")
        else:
            key = os.environ.get("ALPACA_LIVE_KEY")
            secret = os.environ.get("ALPACA_LIVE_SECRET")

        if not key or not secret:
            log.warning(f"[{cfg.mode}] missing API keys in env; leg disabled")
            self.client = None
            self.data = None
            self.cfg.enabled = False
            return

        self._api_key = key
        self._api_secret = secret
        self._base_url = self.PAPER_URL if cfg.mode == "paper" else self.LIVE_URL

        if not _ALPACA_AVAILABLE:
            self.client = None
            self.data = None
            return

        self.client = TradingClient(key, secret, paper=(cfg.mode == "paper"))
        self.data = StockHistoricalDataClient(key, secret)

    @property
    def _key_state(self) -> dict:
        return self.state.setdefault(self.cfg.mode, {})

    def _rest_json(
        self,
        method: str,
        url: str,
        payload: Optional[dict] = None,
        timeout: int = 30,
    ) -> dict | list:
        headers = {
            "APCA-API-KEY-ID": self._api_key,
            "APCA-API-SECRET-KEY": self._api_secret,
            "Content-Type": "application/json",
        }
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"alpaca {method} {url} failed: {exc.code} {body}") from exc
        return json.loads(raw) if raw else {}

    # ---- account state -----------------------------------------------------
    def equity(self) -> Optional[float]:
        if not self.cfg.enabled:
            return None
        if not self.client:
            try:
                payload = self._rest_json("GET", f"{self._base_url}/v2/account")
                return float(payload["equity"])
            except Exception as e:
                log.error(f"[{self.cfg.mode}] REST equity fetch failed: {e}")
                return None
        try:
            return float(self.client.get_account().equity)
        except Exception as e:
            log.error(f"[{self.cfg.mode}] equity fetch failed: {e}")
            return None

    def positions(self) -> dict:
        if not self.cfg.enabled:
            return {}
        if not self.client:
            try:
                out = {}
                for p in self._rest_json("GET", f"{self._base_url}/v2/positions"):
                    out[p["symbol"]] = {
                        "qty": float(p["qty"]),
                        "notional": float(p["market_value"]),
                        "avg_entry": float(p["avg_entry_price"]),
                        "unrealized_pl": float(p["unrealized_pl"]),
                    }
                return out
            except Exception as e:
                log.error(f"[{self.cfg.mode}] REST positions fetch failed: {e}")
                return {}
        try:
            out = {}
            for p in self.client.get_all_positions():
                out[p.symbol] = {
                    "qty": float(p.qty),
                    "notional": float(p.market_value),
                    "avg_entry": float(p.avg_entry_price),
                    "unrealized_pl": float(p.unrealized_pl),
                }
            return out
        except Exception as e:
            log.error(f"[{self.cfg.mode}] positions fetch failed: {e}")
            return {}

    def open_orders(self) -> list[dict]:
        """Open (not yet filled / not yet cancelled) orders for this leg.

        B13: reconcile() counts an open order's UNFILLED quantity toward
        the symbol's actual holding, because the strategy records the
        expected position the moment an order is submitted — a limit
        order that has not filled yet is not a position (positions()
        only reflects fills), so without this the first live run with
        an unfilled order would trip the reconcile drift halt. An
        open order the strategy does NOT expect is still drift (it
        shows up in the same comparison). Returns [] on any fetch
        failure (fail-closed: reconcile then sees the unfilled order
        as missing and halts, rather than masking drift).
        Each entry: {"id", "symbol", "side", "qty", "filled_qty",
        "status", "limit_price", "filled_avg_price"}. The price fields
        are None when the broker leaves them unset (a market order has
        no limit_price; an unfilled order has no filled_avg_price) —
        never 0.0. B15: sizing needs them to price the reservation for
        an open buy order's unfilled quantity (limit first, then
        filled average, then the current last price).
        """
        if not self.cfg.enabled:
            return []
        try:
            if self.client:
                from alpaca.trading.requests import GetOrdersRequest
                from alpaca.trading.enums import QueryOrderStatus

                raw = self.client.get_orders(
                    GetOrdersRequest(status=QueryOrderStatus.OPEN))
                return [{
                    "id": str(o.id),
                    "client_order_id": str(getattr(o, "client_order_id", "") or ""),
                    "symbol": str(o.symbol),
                    "side": str(o.side.value if hasattr(o.side, "value") else o.side).lower(),
                    "qty": float(o.qty),
                    "filled_qty": float(o.filled_qty or 0.0),
                    "status": str(o.status.value if hasattr(o.status, "value") else o.status).lower(),
                    # B16 correction: order_type lets the strategy tell
                    # a trailing stop from a strategy order (the exit
                    # cancels the stop before selling); client_order_id
                    # lets it cancel by idempotency key.
                    "order_type": str(
                        o.order_type.value
                        if hasattr(getattr(o, "order_type", None), "value")
                        else getattr(o, "order_type", "") or "").lower(),
                    "limit_price": _opt_float(getattr(o, "limit_price", None)),
                    "filled_avg_price": _opt_float(getattr(o, "filled_avg_price", None)),
                } for o in raw]
            raw = self._rest_json("GET", f"{self._base_url}/v2/orders?status=open")
            return [{
                "id": str(o.get("id")),
                "client_order_id": str(o.get("client_order_id") or ""),
                "symbol": str(o.get("symbol")),
                "side": str(o.get("side", "")).lower(),
                "qty": float(o.get("qty") or 0.0),
                "filled_qty": float(o.get("filled_qty") or 0.0),
                "status": str(o.get("status", "")).lower(),
                "order_type": str(o.get("type", "") or "").lower(),
                "limit_price": _opt_float(o.get("limit_price")),
                "filled_avg_price": _opt_float(o.get("filled_avg_price")),
            } for o in raw]
        except Exception as e:  # noqa: BLE001 - any fetch failure is fail-closed
            log.error(f"[{self.cfg.mode}] open orders fetch failed: {e}")
            return []

    def is_market_open(self) -> Optional[bool]:
        if not self.cfg.enabled:
            return None
        if not self.client:
            try:
                payload = self._rest_json("GET", f"{self._base_url}/v2/clock")
                return bool(payload.get("is_open"))
            except Exception as e:
                log.error(f"[{self.cfg.mode}] REST clock fetch failed: {e}")
                return None
        try:
            return bool(self.client.get_clock().is_open)
        except Exception as e:
            log.error(f"[{self.cfg.mode}] clock fetch failed: {e}")
            return None

    # ---- the capital floor -------------------------------------------------
    def floor_tripped(self) -> bool:
        return self._key_state.get("floor_tripped", False)

    def check_floor(self) -> bool:
        """
        Returns True if the floor is (or just became) tripped. On the live leg,
        tripping liquidates everything and halts permanently. Paper has a floor
        too but, by design, we let it keep running as a benchmark unless you
        explicitly enable its floor behavior.
        """
        if self.floor_tripped():
            return True
        eq = self.equity()
        if eq is None:
            return False
        floor_value = self._key_state["starting_capital"] * self.cfg.floor_pct
        if eq <= floor_value:
            log.critical(
                f"[{self.cfg.mode}] CAPITAL FLOOR HIT: equity {eq:.2f} "
                f"<= floor {floor_value:.2f}"
            )
            if self.cfg.mode == "live":
                self._liquidate_all()
                self._key_state["floor_tripped"] = True
                self._key_state["tripped_at"] = datetime.now(timezone.utc).isoformat()
            return True
        return False

    def _liquidate_all(self) -> None:
        if not self.cfg.enabled:
            return
        if not self.client:
            try:
                self._rest_json("DELETE", f"{self._base_url}/v2/positions?cancel_orders=true")
                log.critical(f"[{self.cfg.mode}] ALL POSITIONS LIQUIDATED")
            except Exception as e:
                log.critical(f"[{self.cfg.mode}] REST LIQUIDATION FAILED: {e} — manual action needed")
            return
        try:
            self.client.close_all_positions(cancel_orders=True)
            log.critical(f"[{self.cfg.mode}] ALL POSITIONS LIQUIDATED")
        except Exception as e:
            log.critical(f"[{self.cfg.mode}] LIQUIDATION FAILED: {e} — manual action needed")

    # ---- market data (shared logic; both legs can quote) -------------------
    def latest_quote(self, symbol: str) -> Optional[dict]:
        if not self.cfg.enabled:
            return None
        quote = None
        if not self.data:
            try:
                feed = os.environ.get("MARKET_DATA_FEED", "iex")
                params = urllib.parse.urlencode({"feed": feed})
                url = f"https://data.alpaca.markets/v2/stocks/{symbol}/quotes/latest?{params}"
                payload = self._rest_json("GET", url)
                q = payload["quote"]
                bid = float(q.get("bp", 0.0))
                ask = float(q.get("ap", 0.0))
                timestamp = q.get("t")
                quote = {"bid": bid, "ask": ask, "spread": ask - bid, "timestamp": timestamp}
            except Exception as e:
                log.error(f"[{self.cfg.mode}] REST quote failed for {symbol}: {e}")
                return None
        else:
            try:
                req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
                q = self.data.get_stock_latest_quote(req)[symbol]
                timestamp = getattr(q, "timestamp", None)
                timestamp = timestamp.isoformat() if hasattr(timestamp, "isoformat") else timestamp
                quote = {"bid": float(q.bid_price), "ask": float(q.ask_price),
                        "spread": float(q.ask_price) - float(q.bid_price),
                        "timestamp": timestamp}
            except Exception as e:
                log.error(f"[{self.cfg.mode}] quote failed for {symbol}: {e}")
                return None
        # Feed the Guardian's DataClock with the quote's FEED timestamp
        # (not the receipt time): a quote received just now with an old
        # feed timestamp is stale data.
        clock = getattr(self, "data_clock", None)
        if clock is not None and quote is not None:
            clock.note(quote.get("timestamp"))
        return quote

    # ---- order placement ---------------------------------------------------
    def can_trade(self) -> bool:
        if not self.cfg.enabled:
            return False
        if self.cfg.mode == "live" and self.check_floor():
            return False
        return True

    def submit(self, order: dict) -> Optional[dict]:
        """
        order = {symbol, side, qty, order_type: 'market'|'limit', limit_price?,
                 time_in_force?, extended_hours?, client_order_id?}
        client_order_id is the idempotency key (see order_manager.py):
        pass it through so a retry after a timeout reuses the same ID.
        Enforces the single-position cap before sending.
        """
        if not self.can_trade():
            log.warning(f"[{self.cfg.mode}] submit blocked (disabled or floor)")
            return None
        order_options = _normalized_order_options(order)

        # position cap check (buys only)
        if order["side"] == "buy":
            eq = self.equity() or 0
            px = order.get("limit_price") or (self.latest_quote(order["symbol"]) or {}).get("ask", 0)
            notional = order["qty"] * (px or 0)
            existing = self.positions().get(order["symbol"], {}).get("notional", 0.0)
            if eq > 0 and (existing + notional) / eq > self.max_position_pct:
                log.warning(f"[{self.cfg.mode}] {order['symbol']} exceeds "
                            f"{self.max_position_pct:.0%} position cap; rejected")
                return None

        try:
            if not self.client or order_options["extended_hours"]:
                payload = _rest_order_payload(order, order_options)
                result = self._rest_json("POST", f"{self._base_url}/v2/orders", payload)
                return {
                    "id": str(result.get("id")),
                    "symbol": order["symbol"],
                    "status": str(result.get("status")),
                    "submitted": order,
                }

            side = OrderSide.BUY if order["side"] == "buy" else OrderSide.SELL
            client_order_id = order.get("client_order_id")
            if order["order_type"] == "limit":
                req = LimitOrderRequest(
                    symbol=order["symbol"], qty=order["qty"], side=side,
                    time_in_force=_time_in_force_enum(order_options["time_in_force"]),
                    limit_price=order["limit_price"],
                    **({"client_order_id": str(client_order_id)} if client_order_id else {}),
                )
            else:
                req = MarketOrderRequest(
                    symbol=order["symbol"], qty=order["qty"], side=side,
                    time_in_force=_time_in_force_enum(order_options["time_in_force"]),
                    **({"client_order_id": str(client_order_id)} if client_order_id else {}),
                )
            result = self.client.submit_order(req)
            return {"id": str(result.id), "symbol": order["symbol"],
                    "status": str(result.status), "submitted": order}
        except Exception as e:
            log.error(f"[{self.cfg.mode}] order failed: {e}")
            raise  # let the guardian count this as a broker error

    def get_by_client_order_id(self, client_order_id: str) -> Optional[dict]:
        """Lookup one broker order by its client_order_id (B3 reconcile).

        Returns {"id", "symbol", "status", "filled_qty", "limit_price",
        "filled_avg_price"} when the broker has the order, or None
        when it does not (404). A 404 is INCONCLUSIVE for reconcile —
        it is not proof the broker never accepted the first attempt —
        so None is the correct return, not an exception. Non-404
        errors propagate; OrderManager treats a raising lookup as
        inconclusive too.

        B17.6: the prices are part of the return on BOTH paths. The
        sizing reservation prices a DONE_FOR_DAY market buy from its
        filled average (the best estimate for the unfilled remainder),
        not from last_price/entry_price — and the stop-fill P&L
        attribution (B17.3) reads a stop's filled_avg_price through
        this same lookup. A market order has no limit_price (None);
        an unfilled order has no filled_avg_price (None) — callers
        treat None as "no price", never as 0.0.
        """
        if not self.client:
            try:
                result = self._rest_json(
                    "GET", f"{self._base_url}/v2/orders:client_order_id/{client_order_id}"
                )
            except RuntimeError as exc:
                if "404" in str(exc):
                    return None
                raise
            if not isinstance(result, dict) or not result.get("id"):
                return None
            return {
                "id": str(result.get("id")),
                "symbol": result.get("symbol"),
                "status": str(result.get("status", "")),
                "filled_qty": float(result.get("filled_qty") or 0.0),
                "limit_price": _opt_float(result.get("limit_price")),
                "filled_avg_price": _opt_float(result.get("filled_avg_price")),
            }
        try:
            order = self.client.get_order_by_client_order_id(client_order_id)
        except Exception as exc:  # noqa: BLE001 - 404 vs everything else
            if "404" in str(exc):
                return None
            raise
        if order is None:
            return None
        return {
            "id": str(order.id),
            "symbol": order.symbol,
            "status": str(order.status),
            "filled_qty": float(getattr(order, "filled_qty", 0.0) or 0.0),
            "limit_price": _opt_float(getattr(order, "limit_price", None)),
            "filled_avg_price": _opt_float(getattr(order, "filled_avg_price", None)),
        }

    def list_orders(self, status: str = "all", limit: int = 500) -> list[dict]:
        """List the account's orders (B17.1 independent confirmation).

        This is the broker's OWN order history for the account — the
        independent check a human resolution uses when the per-order
        lookup is inconclusive. `status="all"` (the default) returns
        the full history (open, filled, canceled, expired, rejected),
        which is the only list that can confirm an order was never
        accepted: an open-orders-only list coming back empty proves
        nothing (a filled/canceled order is also absent from it).
        Returns one dict per order in the same shape as
        get_by_client_order_id (with client_order_id). An empty list
        is a real answer here — it means the account has no orders
        matching the query — but the caller must still record the
        query's scope (status, window) alongside it, so the audit
        trail shows the check actually covered the submission.
        """
        if self.client:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus

            orders = self.client.get_orders(
                GetOrdersRequest(status=QueryOrderStatus.ALL if status == "all" else QueryOrderStatus.OPEN,
                                 limit=limit)
            )
            out = []
            for order in orders or []:
                out.append({
                    "id": str(getattr(order, "id", "")),
                    "client_order_id": getattr(order, "client_order_id", None),
                    "symbol": getattr(order, "symbol", None),
                    "status": str(getattr(order, "status", "")),
                    "filled_qty": float(getattr(order, "filled_qty", 0.0) or 0.0),
                    "limit_price": _opt_float(getattr(order, "limit_price", None)),
                    "filled_avg_price": _opt_float(getattr(order, "filled_avg_price", None)),
                })
            return out
        params = {"status": status, "limit": str(limit)}
        result = self._rest_json("GET", f"{self._base_url}/v2/orders", params=params)
        if not isinstance(result, list):
            return []
        out = []
        for order in result:
            if not isinstance(order, dict):
                continue
            out.append({
                "id": str(order.get("id", "")),
                "client_order_id": order.get("client_order_id"),
                "symbol": order.get("symbol"),
                "status": str(order.get("status", "")),
                "filled_qty": float(order.get("filled_qty") or 0.0),
                "limit_price": _opt_float(order.get("limit_price")),
                "filled_avg_price": _opt_float(order.get("filled_avg_price")),
            })
        return out

    def cancel_order(self, order_id=None, client_order_id=None) -> Optional[dict]:
        """Cancel one open order (B16: an exit cancels the working entry
        order first). Returns the broker's final view of the order
        {"id", "symbol", "status", "filled_qty"}, or None when the
        broker has no such order (404 on the DELETE — already
        cancelled/expired/gone, which is success: nothing remains to
        fill). Non-404 errors propagate; the caller fail-closes (no
        exit) rather than selling shares that may not exist.

        The SDK's cancel call returns None (no order back), so the
        final state comes from a follow-up GET; if that GET fails the
        cancel itself is still confirmed by the successful DELETE,
        and the returned filled_qty is None (the caller then uses its
        pre-cancel snapshot). Disabled legs return None (nothing to
        cancel — the leg never submits).
        """
        if not self.cfg.enabled:
            return None
        if order_id is None and client_order_id is None:
            return None
        if order_id is not None:
            if self.client:
                try:
                    self.client.cancel_order_by_id(order_id)
                except Exception as exc:  # noqa: BLE001 - 404 vs everything else
                    if "404" in str(exc):
                        return None
                    raise
            else:
                try:
                    self._rest_json("DELETE", f"{self._base_url}/v2/orders/{order_id}")
                except RuntimeError as exc:
                    if "404" in str(exc):
                        return None
                    raise
            try:
                fetched = self._fetch_order_by_id(order_id)
            except Exception:  # noqa: BLE001 - cancel confirmed; GET is best-effort
                fetched = None
            if fetched is not None:
                return fetched
            return {"id": str(order_id), "symbol": None, "status": "cancelled",
                    "filled_qty": None}
        # Cancel by client_order_id (REST only — this SDK version has no
        # cancel-by-client-order-id call).
        try:
            self._rest_json(
                "DELETE",
                f"{self._base_url}/v2/orders:client_order_id/{client_order_id}",
            )
        except RuntimeError as exc:
            if "404" in str(exc):
                return None
            raise
        fetched = self.get_by_client_order_id(client_order_id)
        if fetched is not None:
            return fetched
        return {"id": None, "symbol": None, "status": "cancelled",
                "filled_qty": None, "client_order_id": str(client_order_id)}

    def _fetch_order_by_id(self, order_id) -> Optional[dict]:
        """GET one order by broker id (after a cancel), normalized like
        get_by_client_order_id. None on 404 (gone)."""
        if self.client:
            try:
                order = self.client.get_order_by_id(order_id)
            except Exception as exc:  # noqa: BLE001 - 404 vs everything else
                if "404" in str(exc):
                    return None
                raise
            if order is None:
                return None
            return {
                "id": str(order.id),
                "symbol": order.symbol,
                "status": _status_str(order.status),
                "filled_qty": float(getattr(order, "filled_qty", 0.0) or 0.0),
            }
        try:
            result = self._rest_json("GET", f"{self._base_url}/v2/orders/{order_id}")
        except RuntimeError as exc:
            if "404" in str(exc):
                return None
            raise
        if not isinstance(result, dict) or not result.get("id"):
            return None
        return {
            "id": str(result.get("id")),
            "symbol": result.get("symbol"),
            "status": str(result.get("status", "")),
            "filled_qty": float(result.get("filled_qty") or 0.0),
        }

    def attach_trailing_stop(self, symbol: str, qty: float, trail_pct: float,
                             client_order_id: str | None = None) -> Optional[dict]:
        """Place a trailing stop SELL to protect an open long position.

        B17.2 / Detail 1: when `client_order_id` is given (the strategy
        passes a deterministic "<entry intent id>-trail"), the stop is
        submitted with that idempotency key, so its final state can be
        confirmed BY ORDER ID (get_by_client_order_id) — never inferred
        from its absence in the open-orders list. The returned dict
        carries the client_order_id so the caller can record it.
        """
        if not self.can_trade():
            return None
        try:
            if not self.client:
                payload = {
                    "symbol": symbol,
                    "qty": str(qty),
                    "side": "sell",
                    "type": "trailing_stop",
                    "time_in_force": "gtc",
                    "trail_percent": str(trail_pct),
                }
                if client_order_id:
                    payload["client_order_id"] = str(client_order_id)
                result = self._rest_json("POST", f"{self._base_url}/v2/orders", payload)
                return {"id": str(result.get("id")), "symbol": symbol, "trail_pct": trail_pct,
                        "client_order_id": client_order_id}

            req = TrailingStopOrderRequest(
                symbol=symbol, qty=qty, side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC, trail_percent=trail_pct,
                **({"client_order_id": str(client_order_id)} if client_order_id else {}),
            )
            result = self.client.submit_order(req)
            return {"id": str(result.id), "symbol": symbol, "trail_pct": trail_pct,
                    "client_order_id": client_order_id}
        except Exception as e:
            log.error(f"[{self.cfg.mode}] trailing stop failed for {symbol}: {e}")
            return None


def decide_order_type(signal_direction: str, quote: Optional[dict],
                      max_spread_pct: float = 0.005,
                      side: str = "buy") -> dict:
    """
    The market-vs-limit hybrid logic.

    Principle you described:
      - Signal says UP and we want in -> buy at MARKET (don't miss the move).
      - Signal says the entry is risky / price drifting -> use a LIMIT at/near
        the bid so we don't overpay, accepting we might not fill.
      - If the spread is too wide, prefer LIMIT regardless (don't pay the spread).

    Returns {order_type, limit_price?} given the live quote. Unsafe or missing
    quotes fail closed instead of silently becoming market orders.
    """
    if side not in {"buy", "sell"}:
        return {"order_type": "skip", "reason": "invalid_side"}

    if quote is None:
        return {"order_type": "skip", "reason": "missing_quote"}

    try:
        bid = float(quote["bid"])
        ask = float(quote["ask"])
    except (KeyError, TypeError, ValueError):
        return {"order_type": "skip", "reason": "invalid_quote"}
    if bid <= 0 or ask <= 0 or ask < bid:
        return {"order_type": "skip", "reason": "invalid_quote"}

    mid = (bid + ask) / 2
    spread_pct = (ask - bid) / mid if mid > 0 else 1.0
    passive_price = bid if side == "buy" else ask

    # Wide spread -> never cross it at market; quote on the passive side.
    if spread_pct > max_spread_pct:
        return {"order_type": "limit", "limit_price": round(passive_price, 2)}

    urgent = (
        side == "buy" and signal_direction == "strong_up"
    ) or (
        side == "sell" and signal_direction == "strong_down"
    )
    if urgent:
        # Conviction + tight spread -> take it at market.
        return {"order_type": "market"}

    # Mild/uncertain signal -> passive limit, accepting that it may not fill.
    return {"order_type": "limit", "limit_price": round(passive_price, 2)}


class DualBroker:
    """Drives paper and live legs together from one set of decisions."""

    def __init__(self, cfg: BrokerConfig):
        self.cfg = cfg
        self.state = self._load_state()
        self.paper = AlpacaLeg(cfg.paper, cfg.max_position_pct, self.state)
        self.live = AlpacaLeg(cfg.live, cfg.max_position_pct, self.state)

    def _load_state(self) -> dict:
        p = Path(self.cfg.state_path)
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                pass
        return {}

    def save_state(self) -> None:
        Path(self.cfg.state_path).write_text(json.dumps(self.state, indent=2))

    def status(self) -> dict:
        return {
            "paper": {"equity": self.paper.equity(),
                      "can_trade": self.paper.can_trade(),
                      "market_open": self.paper.is_market_open(),
                      "positions": len(self.paper.positions())},
            "live": {"equity": self.live.equity(),
                     "can_trade": self.live.can_trade(),
                     "market_open": self.live.is_market_open(),
                     "floor_tripped": self.live.floor_tripped(),
                     "positions": len(self.live.positions())},
        }

    def is_market_open(self) -> bool:
        paper_open = self.paper.is_market_open()
        live_open = self.live.is_market_open()
        return bool(paper_open or live_open)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("alpaca-py available:", _ALPACA_AVAILABLE)
    print("\nThis module needs API keys in env vars to connect live. Without them,")
    print("legs disable themselves gracefully (no crash). Testing the order-type")
    print("logic, which needs no keys:\n")

    cases = [
        ("strong_up", {"bid": 199.98, "ask": 200.00, "spread": 0.02}),  # tight, conviction
        ("mild_up",   {"bid": 199.98, "ask": 200.00, "spread": 0.02}),  # tight, mild
        ("strong_up", {"bid": 195.00, "ask": 200.00, "spread": 5.00}),  # wide spread
        ("strong_up", None),                                             # no quote
    ]
    for direction, quote in cases:
        decision = decide_order_type(direction, quote)
        sp = f"{quote['spread']/((quote['bid']+quote['ask'])/2):.2%}" if quote else "n/a"
        print(f"  {direction:<10} spread={sp:<6} -> {decision}")
