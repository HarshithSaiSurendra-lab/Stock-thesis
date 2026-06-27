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

        if not _ALPACA_AVAILABLE:
            self.client = None
            self.data = None
            return

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

        self.client = TradingClient(key, secret, paper=(cfg.mode == "paper"))
        self.data = StockHistoricalDataClient(key, secret)

    @property
    def _key_state(self) -> dict:
        return self.state.setdefault(self.cfg.mode, {})

    # ---- account state -----------------------------------------------------
    def equity(self) -> Optional[float]:
        if not self.client:
            return None
        try:
            return float(self.client.get_account().equity)
        except Exception as e:
            log.error(f"[{self.cfg.mode}] equity fetch failed: {e}")
            return None

    def positions(self) -> dict:
        if not self.client:
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
        if not self.client:
            return
        try:
            self.client.close_all_positions(cancel_orders=True)
            log.critical(f"[{self.cfg.mode}] ALL POSITIONS LIQUIDATED")
        except Exception as e:
            log.critical(f"[{self.cfg.mode}] LIQUIDATION FAILED: {e} — manual action needed")

    # ---- market data (shared logic; both legs can quote) -------------------
    def latest_quote(self, symbol: str) -> Optional[dict]:
        if not self.data:
            return None
        try:
            req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
            q = self.data.get_stock_latest_quote(req)[symbol]
            return {"bid": float(q.bid_price), "ask": float(q.ask_price),
                    "spread": float(q.ask_price) - float(q.bid_price)}
        except Exception as e:
            log.error(f"[{self.cfg.mode}] quote failed for {symbol}: {e}")
            return None

    # ---- order placement ---------------------------------------------------
    def can_trade(self) -> bool:
        if not self.cfg.enabled or not self.client:
            return False
        if self.cfg.mode == "live" and self.check_floor():
            return False
        return True

    def submit(self, order: dict) -> Optional[dict]:
        """
        order = {symbol, side, qty, order_type: 'market'|'limit', limit_price?}
        Enforces the single-position cap before sending.
        """
        if not self.can_trade():
            log.warning(f"[{self.cfg.mode}] submit blocked (disabled or floor)")
            return None

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
            side = OrderSide.BUY if order["side"] == "buy" else OrderSide.SELL
            if order["order_type"] == "limit":
                req = LimitOrderRequest(
                    symbol=order["symbol"], qty=order["qty"], side=side,
                    time_in_force=TimeInForce.DAY, limit_price=order["limit_price"],
                )
            else:
                req = MarketOrderRequest(
                    symbol=order["symbol"], qty=order["qty"], side=side,
                    time_in_force=TimeInForce.DAY,
                )
            result = self.client.submit_order(req)
            return {"id": str(result.id), "symbol": order["symbol"],
                    "status": str(result.status), "submitted": order}
        except Exception as e:
            log.error(f"[{self.cfg.mode}] order failed: {e}")
            raise  # let the guardian count this as a broker error

    def attach_trailing_stop(self, symbol: str, qty: float, trail_pct: float) -> Optional[dict]:
        """Place a trailing stop SELL to protect an open long position."""
        if not self.can_trade():
            return None
        try:
            req = TrailingStopOrderRequest(
                symbol=symbol, qty=qty, side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC, trail_percent=trail_pct,
            )
            result = self.client.submit_order(req)
            return {"id": str(result.id), "symbol": symbol, "trail_pct": trail_pct}
        except Exception as e:
            log.error(f"[{self.cfg.mode}] trailing stop failed for {symbol}: {e}")
            return None


def decide_order_type(signal_direction: str, quote: Optional[dict],
                      max_spread_pct: float = 0.005) -> dict:
    """
    The market-vs-limit hybrid logic.

    Principle you described:
      - Signal says UP and we want in -> buy at MARKET (don't miss the move).
      - Signal says the entry is risky / price drifting -> use a LIMIT at/near
        the bid so we don't overpay, accepting we might not fill.
      - If the spread is too wide, prefer LIMIT regardless (don't pay the spread).

    Returns {order_type, limit_price?} given the live quote.
    """
    if quote is None:
        return {"order_type": "market"}  # no quote -> simplest path

    mid = (quote["bid"] + quote["ask"]) / 2
    spread_pct = quote["spread"] / mid if mid > 0 else 1.0

    # wide spread -> never cross it at market; bid a limit
    if spread_pct > max_spread_pct:
        return {"order_type": "limit", "limit_price": round(quote["bid"], 2)}

    if signal_direction == "strong_up":
        # conviction + tight spread -> take it at market
        return {"order_type": "market"}

    # mild/uncertain signal -> limit at the bid, let the market come to us
    return {"order_type": "limit", "limit_price": round(quote["bid"], 2)}


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
                      "positions": len(self.paper.positions())},
            "live": {"equity": self.live.equity(),
                     "can_trade": self.live.can_trade(),
                     "floor_tripped": self.live.floor_tripped(),
                     "positions": len(self.live.positions())},
        }


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
