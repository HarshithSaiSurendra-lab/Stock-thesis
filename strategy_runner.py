from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
import time

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from broker_alpaca import decide_order_type
from config import TradingConfig
from execution_journal import ExecutionJournal
from filters import downside_ok, market_regime, volatility_scale
from indicators import build_feature_frame
from memory import SignalSnapshot, TradeMemory
from notifier import Notifier, format_daily
from order_manager import OrderManager, build_intent, compute_inputs_hash
from reconcile import _open_orders_for_leg, reconcile
from sector_map import sector_benchmark_for, sector_benchmarks_for
from trade_signal import composite_signal, composite_score, trend_quality_score
from universe import StaleBarsError, fetch_symbol_frame, select_universe

log = logging.getLogger("strategy")


@dataclass
class StrategyState:
    trading_day: str = ""
    submitted_today: list[str] = field(default_factory=list)
    expected_positions: dict = field(
        default_factory=lambda: {"paper": {}, "live": {}}
    )
    open_entries: dict = field(default_factory=dict)
    last_run_ts: Optional[str] = None

    @classmethod
    def load(cls, path: str) -> "StrategyState":
        p = Path(path)
        if p.exists():
            try:
                payload = json.loads(p.read_text())
                return cls(**payload)
            except Exception:
                pass
        return cls()

    def save(self, path: str) -> None:
        Path(path).write_text(json.dumps(self.__dict__, indent=2))

    def start_day(self, day: str) -> None:
        if self.trading_day != day:
            self.trading_day = day
            self.submitted_today = []


def _latest_row(features: pd.DataFrame) -> Optional[pd.Series]:
    if features.empty:
        return None
    row = features.dropna(how="all").iloc[-1] if not features.dropna(how="all").empty else None
    return row


def _fill_price(quote: Optional[dict], direction: str = "buy") -> float:
    if quote is None:
        return float("nan")
    if direction == "sell":
        return float(quote.get("bid") or quote.get("ask") or 0.0)
    return float(quote.get("ask") or quote.get("bid") or 0.0)


def _quote_with_spread_pct(quote: Optional[dict]) -> Optional[dict]:
    if quote is None:
        return None
    out = dict(quote)
    bid = float(out.get("bid", 0.0) or 0.0)
    ask = float(out.get("ask", 0.0) or 0.0)
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2
    if "spread_pct" not in out and mid > 0:
        out["spread_pct"] = float(out.get("spread", 0.0)) / mid
    return out


def _entry_price(quote: Optional[dict], row: pd.Series) -> float:
    if quote is not None:
        ask = float(quote.get("ask", 0.0) or 0.0)
        if ask > 0:
            return ask
    return float(row["close"])


def _decision_snapshot(symbol: str, direction: str, row: pd.Series, quote: Optional[dict]) -> SignalSnapshot:
    spread_pct = quote.get("spread_pct") if quote else None
    dollar_volume = float(row.get("close", 0.0) * row.get("volume", 0.0)) if "volume" in row else 0.0
    return SignalSnapshot(
        symbol=symbol,
        direction=direction,
        rsi=float(row.get("rsi_14", float("nan"))),
        mfi=float(row.get("mfi_14", float("nan"))),
        kvo_hist=float(row.get("kvo_hist", float("nan"))),
        obv_slope=float(row.get("obv_slope_20", float("nan"))),
        wad_slope=float(row.get("wad_slope_20", float("nan"))),
        momentum=float(row.get("mom_126_21", float("nan"))),
        rvol=float(row.get("rvol_20", float("nan"))),
        spread_pct=float(spread_pct if spread_pct is not None else float("nan")),
        dollar_volume=float(dollar_volume),
    )


def _side_for_signal(direction: str) -> str:
    return "buy" if direction in {"strong_up", "mild_up"} else "sell"


def _build_order(symbol: str, side: str, qty: float, quote: Optional[dict], direction: str, cfg: TradingConfig) -> dict:
    order = {"symbol": symbol, "side": side, "qty": qty}
    if side == "buy":
        order.update(decide_order_type(direction, quote, cfg.universe.max_spread_pct))
    else:
        order["order_type"] = "market"
    return order


# B13: the live loop's B3 intent namespace. The live strategy is the
# trend engine in this file (composite_signal / trend_quality_score);
# it is NOT one of the registered LLM designs (v2, v3, h3-lazy-veto,
# h7-c1c, exp-ml-ranker-v1), so no registered design_id applies.
# "live-trend" labels the intent namespace only — it makes no claim
# about which design is running and must not be read as a gate result.
LIVE_DESIGN_ID = "live-trend"


def _live_intent(order: dict, inputs: dict, trading_day: str) -> dict:
    """Build the B3 intent for a live-loop order, from the SAME order
    dict the dry-run preview builds (symbol/side/qty/type/limit).

    inputs_hash covers the decision inputs (for entries: direction,
    scores, price, notionals; for exits: the exit reason + qty) so a
    re-derived identical decision maps to the same intent — a restart
    re-deriving the same decision reuses the same client_order_id and
    can never place a second order for it — while a genuinely
    different decision gets a different intent. The callers round the
    inputs to the same precision the decision log records (scores 6dp,
    last_price/notionals 2dp), so the intent is reproducible from the
    log alone (the B13 e2e test does exactly that). It deliberately
    excludes the quote's feed timestamp: the same decision re-derived
    seconds later must still be the same intent.
    """
    return build_intent(
        design_id=LIVE_DESIGN_ID,
        trading_day=trading_day,
        symbol=order["symbol"],
        side=order["side"],
        qty=order["qty"],
        order_type=order.get("order_type", "market"),
        limit_price=order.get("limit_price"),
        inputs_hash=compute_inputs_hash(inputs),
        account="paper",
    )


def _submit_paper_via_manager(order: dict, intent: dict, manager: OrderManager, broker) -> Optional[dict]:
    """place_order_fn for guardian.submit: route the paper leg through
    the B3 OrderManager, inside the guardian's gate and breaker.

    Returns the broker-shaped result dict on success ({"id", "symbol",
    "status", "submitted"} — the shape AlpacaLeg.submit returns, which
    _entry_price_from_result reads), None when no broker order exists
    (REJECTED / BLOCKED), and raises TimeoutError when the outcome is
    UNKNOWN — exactly today's timeout path: guardian.submit counts it
    as a broker error and returns None, while the intent stays UNKNOWN
    with a persistent alert and the symbol blocked.
    """
    def _place(submitted: dict) -> dict:
        result = broker.paper.submit(submitted)
        if result is None:
            # The paper leg refused the order without placing it
            # (disabled, floor, or position cap): conclusive, no order
            # exists. Raise an explicit rejection so OrderManager marks
            # it REJECTED (terminal, no block) instead of UNKNOWN.
            raise RuntimeError(
                "paper leg rejected the order without placing it "
                "(disabled, floor, or position cap)"
            )
        return result

    lookup_fn = getattr(broker.paper, "get_by_client_order_id", None)
    outcome = manager.submit(
        intent,
        place_order_fn=_place,
        lookup_fn=lookup_fn,
        grace_seconds=0.0,
        max_polls=3,
        sleep_fn=lambda s: None,
    )
    status = outcome["status"]
    if status in ("SUBMITTED", "PARTIALLY_FILLED", "FILLED") and outcome.get("broker_order_id"):
        return {
            "id": outcome["broker_order_id"],
            "symbol": order["symbol"],
            "status": status.lower(),
            "submitted": order,
        }
    if status == "UNKNOWN":
        raise TimeoutError(
            f"paper order outcome unknown for {order['symbol']}: intent "
            f"{intent['intent_id']} UNKNOWN after lookup; alert recorded, "
            "symbol blocked"
        )
    # REJECTED / BLOCKED: no broker order exists. Return None — the same
    # as today's None-return path (guardian records no error, the loop
    # treats it as no fill). The BLOCKED case is normally pre-checked
    # by the caller (is_blocked) and never reaches here.
    return None


def _mirror_cancelled_intents_into_state(state: StrategyState, intents: list[dict]) -> list[dict]:
    """Mirror terminally-cancelled intents into the strategy state (B16).

    A DAY order the broker cancelled or expired since submit leaves a
    stale expected position in the state: the entry was recorded at
    submit time, and nothing popped it. The next run's end-of-run
    reconcile would then see expected qty vs actual (filled + open 0)
    and trip the drift halt — fail-closed, but firing on the ROUTINE
    expiry case. For each CANCELLED intent, find the matching open
    entry (by the intent_id recorded at submit time) and reduce the
    expected position to the intent's filled qty (removing it when
    nothing filled); a partially filled entry keeps the filled qty as
    its position. The intent store remains the source of truth for
    order status; the state mirrors it.

    Entries with no recorded intent_id (recorded before B16) are
    skipped — the end-of-run reconcile remains the backstop for them
    and halts on drift rather than silently guessing. Returns one
    adjustment dict per entry changed, for the run summary.
    """
    adjustments = []
    for intent in intents or []:
        if str(intent.get("status")) != "CANCELLED":
            continue
        symbol = str(intent.get("symbol"))
        account = str(intent.get("account") or "paper")
        filled = float(intent.get("filled_qty") or 0.0)
        record = state.open_entries.get(symbol)
        if record is None:
            continue
        if not record.get("intent_id"):
            continue
        if record.get("intent_id") != intent.get("intent_id"):
            continue
        expected = state.expected_positions.get(account, {})
        if filled > 0:
            if symbol in expected:
                expected[symbol]["qty"] = filled
            if account == "paper":
                record["qty"] = filled
            adjustments.append({
                "symbol": symbol, "intent_id": intent.get("intent_id"),
                "filled_qty": filled, "action": "reduced_to_filled",
            })
        else:
            expected.pop(symbol, None)
            if account == "paper" and symbol not in state.expected_positions.get("live", {}):
                state.open_entries.pop(symbol, None)
            adjustments.append({
                "symbol": symbol, "intent_id": intent.get("intent_id"),
                "filled_qty": 0.0, "action": "removed",
            })
    return adjustments


def _cancel_working_orders_on_leg(leg, symbol: str, entry_side: str, *,
                                  manager=None, intent_id=None, lookup_fn=None):
    """Cancel the working entry order(s) for `symbol` on one leg, and
    report how many shares the entry actually holds.

    Returns (ok, filled_qty, reason). ok False means the exit must NOT
    proceed (fail-closed): a cancel failed, or the working order cannot
    be confirmed gone. filled_qty is the shares the entry holds: the
    filled quantity of the cancelled working orders, or the leg's
    position when no working order remains. The end-of-run reconcile
    is the backstop for any residual mismatch.

    The confirmation is a fresh open-orders re-fetch after the cancel:
    no entry-side order for the symbol with remaining qty > 0 may still
    be open. When the entry's intent is still non-terminal in the store
    (paper leg), the broker's per-order lookup must also agree the
    order is terminal — a lookup that returns a WORKING status vetoes,
    and a lookup that raises vetoes too (fail-closed; the exit retries
    next run). A lookup that returns None relies on the re-fetch (it
    can only veto, never falsely confirm).
    """
    try:
        open_orders = leg.open_orders()
    except Exception as exc:  # noqa: BLE001 - fail-closed on any fetch failure
        return False, 0.0, f"open-orders fetch failed: {exc}"
    working = [
        o for o in (open_orders or [])
        if str(o.get("symbol")) == symbol
        and str(o.get("side", "")).lower() == str(entry_side).lower()
        and float(o.get("qty") or 0.0) - float(o.get("filled_qty") or 0.0) > 0
    ]
    filled = 0.0
    for order in working:
        filled += float(order.get("filled_qty") or 0.0)
        cancel = getattr(leg, "cancel_order", None)
        if cancel is None:
            return False, 0.0, (
                f"leg has no cancel_order; cannot cancel {order.get('id')}"
            )
        try:
            result = cancel(order_id=order.get("id"),
                            client_order_id=order.get("client_order_id"))
        except Exception as exc:  # noqa: BLE001 - fail-closed on any cancel failure
            return False, 0.0, f"cancel failed for {order.get('id')}: {exc}"
        if isinstance(result, dict) and result.get("filled_qty") is not None:
            # The cancel result is fresher than the pre-cancel snapshot.
            filled += float(result.get("filled_qty") or 0.0) - float(order.get("filled_qty") or 0.0)
    if working:
        try:
            remaining = leg.open_orders()
        except Exception as exc:  # noqa: BLE001 - fail-closed on any fetch failure
            return False, 0.0, f"open-orders re-fetch failed: {exc}"
        still_open = [
            o for o in (remaining or [])
            if str(o.get("symbol")) == symbol
            and str(o.get("side", "")).lower() == str(entry_side).lower()
            and float(o.get("qty") or 0.0) - float(o.get("filled_qty") or 0.0) > 0
        ]
        if still_open:
            return False, 0.0, (
                "cancel did not take effect: "
                f"{[o.get('id') for o in still_open]} still open"
            )
    else:
        # No working entry order: the entry fully filled (or never
        # existed on this leg). The position is the filled quantity.
        try:
            positions = leg.positions()
        except Exception as exc:  # noqa: BLE001 - fail-closed on any fetch failure
            return False, 0.0, f"positions fetch failed: {exc}"
        filled = abs(float((positions or {}).get(symbol, {}).get("qty", 0.0) or 0.0))
    # Intent-store tripwire (paper leg only): if the entry's intent is
    # still non-terminal, the broker's per-order lookup must agree the
    # order is gone — a WORKING status vetoes the exit.
    if manager is not None and intent_id:
        intent = manager.get_intent(intent_id)
        if (intent is not None
                and intent.get("status") in {"SUBMITTED", "PARTIALLY_FILLED"}
                and intent.get("broker_order_id")):
            if lookup_fn is None:
                return False, 0.0, "no lookup to confirm the entry order is gone"
            try:
                found = lookup_fn(intent_id)
            except Exception as exc:  # noqa: BLE001 - fail-closed; exit retries next run
                return False, 0.0, f"lookup failed for {intent_id}: {exc}"
            if found is not None:
                status = str(found.get("status") or "").lower()
                if status not in {"cancelled", "canceled", "expired", "done_for_day",
                                  "filled", "rejected"}:
                    return False, 0.0, f"entry order {intent_id} still {status} after cancel"
    return True, filled, ""


def _cancel_entry_orders_for_exit(broker, symbol: str, entry_side: str, *,
                                  manager=None, intent_id=None):
    """Cancel the working entry order(s) for `symbol` before an exit,
    on the paper leg and the live leg, and report how many shares the
    entry holds. Returns (ok, filled_qty, reason); ok False means the
    exit must NOT proceed. The paper leg determines the exit size; the
    live leg is cancelled the same way (a no-op while disabled) so the
    behaviour is identical if it is ever enabled."""
    filled = 0.0
    for leg_name in ("paper", "live"):
        leg = getattr(broker, leg_name, None)
        if leg is None:
            continue
        ok, leg_filled, reason = _cancel_working_orders_on_leg(
            leg, symbol, entry_side,
            manager=manager if leg_name == "paper" else None,
            intent_id=intent_id if leg_name == "paper" else None,
            lookup_fn=getattr(leg, "get_by_client_order_id", None)
            if leg_name == "paper" else None,
        )
        if not ok:
            return False, 0.0, f"{leg_name}: {reason}"
        if leg_name == "paper":
            filled = leg_filled
    return True, filled, ""


def _trail_percent_for_row(row: pd.Series, cfg: TradingConfig) -> float:
    if not cfg.exits.dynamic_trail_enabled:
        return cfg.exits.trail_percent
    rvol = float(row.get("rvol_20", float("nan")))
    if not math.isfinite(rvol) or rvol <= 0:
        return cfg.exits.trail_percent
    daily_vol = rvol / math.sqrt(252)
    trail = daily_vol * cfg.exits.dynamic_trail_vol_multiple * 100.0
    return max(cfg.exits.min_trail_percent, min(cfg.exits.max_trail_percent, trail))


def _entry_price_from_result(result: Optional[dict], fallback: float, quote: Optional[dict], side: str) -> float:
    if result and isinstance(result, dict):
        submitted = result.get("submitted", {})
        if "limit_price" in submitted and submitted["limit_price"] is not None:
            return float(submitted["limit_price"])
    if side == "buy":
        return _fill_price(quote, "buy")
    return fallback


def _close_price_from_quote(quote: Optional[dict], side: str) -> float:
    return _fill_price(quote, side)


def _strategy_equity(cfg: TradingConfig, broker_equity: Optional[float]) -> float:
    configured = float(cfg.sizing.strategy_capital)
    if broker_equity is None or broker_equity <= 0:
        return configured
    return min(configured, float(broker_equity))


def _strategy_owned_symbols(state: StrategyState) -> set[str]:
    symbols = set(state.open_entries)
    for leg in ("paper", "live"):
        symbols.update(state.expected_positions.get(leg, {}).keys())
    return symbols


def _positions_notional(positions: dict, symbols: Optional[set[str]] = None) -> float:
    total = 0.0
    for symbol, payload in positions.items():
        if symbols is not None and symbol not in symbols:
            continue
        try:
            total += abs(float(payload.get("notional", 0.0)))
        except (TypeError, ValueError):
            continue
    return total


def _reservation_price_for_order(order: dict, last_prices: dict, entry_prices: dict) -> tuple[Optional[float], Optional[str]]:
    """Price one open buy order's unfilled quantity for its budget
    reservation. The chain is deliberate — no price is ever invented:

    1. limit_price — a limit buy can only fill at or below its limit,
       so the limit IS the worst-case cost of the remainder.
    2. filled_avg_price — a market buy has no limit; the average price
       its filled shares actually got is the best estimate for the
       rest.
    3. last_price — the symbol's current price from data already
       fetched in this run (the candidate's last_price).
    4. entry_price — the price recorded in the strategy state when the
       order was submitted (the fill/entry price observed then). Used
       only when the symbol is not a candidate this run — which is the
       normal case for an open order, since a symbol submitted today is
       deduped out of the candidate list.

    Returns (price, source) or (None, None) when no price in the chain
    is usable; the caller then reserves nothing for that order and
    logs a warning (fail-open, but visible — the pre-B15 behaviour for
    that order, not a silent success).
    """
    for key, source in (("limit_price", "limit_price"),
                        ("filled_avg_price", "filled_avg_price")):
        try:
            value = float(order.get(key)) if order.get(key) is not None else None
        except (TypeError, ValueError):
            value = None
        if value is not None and value > 0:
            return value, source
    symbol = str(order.get("symbol"))
    for prices, source in ((last_prices, "last_price"), (entry_prices, "entry_price")):
        try:
            value = float(prices.get(symbol)) if prices.get(symbol) is not None else None
        except (TypeError, ValueError):
            value = None
        if value is not None and value > 0:
            return value, source
    return None, None


def _open_buy_reservations(open_orders: list, symbols: Optional[set[str]],
                           last_prices: dict, entry_prices: dict) -> tuple[float, list]:
    """Budget already committed to open BUY orders (B15).

    A filled position is counted once, in `paper_positions` at market
    notional; the same order's UNFILLED quantity is counted once here,
    at the reservation price from `_reservation_price_for_order`. The
    filled shares are never in both: the reservation uses remaining =
    qty - filled_qty only. Sells never reserve buy budget (an open
    sell reduces exposure; it does not commit buy capital), and orders
    for symbols the strategy does not own are ignored, exactly like
    `_positions_notional`.

    Returns (total_reservation, detail) where detail is one dict per
    reserved order: symbol, order_id, remaining_qty, price,
    price_source, notional. Orders with no usable price contribute
    nothing and are logged.
    """
    total = 0.0
    detail: list[dict] = []
    for order in open_orders or []:
        if str(order.get("side", "")).lower() != "buy":
            continue
        symbol = str(order.get("symbol"))
        if symbols is not None and symbol not in symbols:
            continue
        try:
            remaining = float(order.get("qty") or 0.0) - float(order.get("filled_qty") or 0.0)
        except (TypeError, ValueError):
            continue
        if remaining <= 0:
            continue
        price, source = _reservation_price_for_order(order, last_prices, entry_prices)
        if price is None:
            log.warning(
                "open buy order %s for %s has no usable price; reserving nothing for it",
                order.get("id"), symbol)
            continue
        notional = remaining * price
        total += notional
        detail.append({
            "symbol": symbol,
            "order_id": str(order.get("id")),
            "remaining_qty": remaining,
            "price": round(price, 2),
            "price_source": source,
            "notional": round(notional, 2),
        })
    return total, detail


def _position_payload_for_validation(positions: dict, symbols: Optional[set[str]] = None) -> dict:
    out = {}
    for symbol, payload in positions.items():
        if symbols is not None and symbol not in symbols:
            continue
        notional = abs(float(payload.get("notional", 0.0) or 0.0))
        out[symbol] = dict(payload, notional=notional)
    return out


def _safe_float(value, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _decision_rank(
    row: pd.Series,
    quote: dict,
    score: float,
    quality: float,
    relative_strength: float,
    sector_relative_strength: float,
    cfg: TradingConfig,
) -> dict:
    momentum = _safe_float(row.get("mom_126_21"), 0.0)
    rvol = _safe_float(row.get("rvol_20"), cfg.risk.max_entry_rvol)
    spread_pct = _safe_float(quote.get("spread_pct"), 1.0)
    dollar_volume = _safe_float(row.get("close"), 0.0) * _safe_float(row.get("volume"), 0.0)
    rel_strength = None if pd.isna(relative_strength) else _safe_float(relative_strength)
    sector_rel_strength = None if pd.isna(sector_relative_strength) else _safe_float(sector_relative_strength)
    rel_component = 0.0 if rel_strength is None else _clamp((rel_strength + 0.05) / 0.20) * 1.5
    sector_rel_component = (
        0.0 if sector_rel_strength is None else _clamp((sector_rel_strength + 0.03) / 0.15) * 1.0
    )

    components = {
        "signal": _safe_float(score),
        "trend": _safe_float(quality) * 0.8,
        "momentum": _clamp(momentum / 0.30) * 2.0,
        "relative_strength": rel_component,
        "sector_relative_strength": sector_rel_component,
        "volatility": _clamp(1.0 - (rvol / max(cfg.risk.max_entry_rvol, 0.01))) * 1.25,
        "spread": _clamp(1.0 - (spread_pct / max(cfg.risk.max_quote_spread_pct, 0.0001))),
        "liquidity": _clamp(math.log10(max(dollar_volume, 1.0) / max(cfg.universe.min_dollar_volume, 1.0)) / 2.0),
    }
    decision_score = sum(components.values())
    return {
        "decision_score": decision_score,
        "components": components,
        "dollar_volume": dollar_volume,
        "spread_pct": spread_pct,
        "rvol_20": rvol,
        "momentum_126_21": momentum,
        "sector_relative_strength_63": sector_relative_strength,
    }


def _decision_report_item(item: dict) -> dict:
    return {
        "symbol": item["symbol"],
        "direction": item["direction"],
        "decision_score": round(item["decision_score"], 3),
        "signal_score": round(item["score"], 3),
        "trend_quality": round(item["trend_quality"], 3),
        "momentum_126_21": round(item["rank"]["momentum_126_21"], 4),
        "relative_strength_63": (
            None if pd.isna(item["relative_strength_63"]) else round(item["relative_strength_63"], 4)
        ),
        "sector_benchmark": item.get("sector_benchmark"),
        "sector_relative_strength_63": (
            None if pd.isna(item["sector_relative_strength_63"]) else round(item["sector_relative_strength_63"], 4)
        ),
        "rvol_20": round(item["rank"]["rvol_20"], 4),
        "spread_pct": round(item["rank"]["spread_pct"], 5),
        "dollar_volume": round(item["rank"]["dollar_volume"], 2),
        "last_price": round(item["last_price"], 2),
        "components": {k: round(v, 3) for k, v in item["rank"]["components"].items()},
    }


def _nullable_float(value) -> Optional[float]:
    out = _safe_float(value, float("nan"))
    return None if pd.isna(out) else out


def _relative_strength_63(row: pd.Series, benchmark_row: Optional[pd.Series]) -> float:
    if benchmark_row is None:
        return float("nan")
    symbol_ret = _safe_float(row.get("ret_63"), float("nan"))
    benchmark_ret = _safe_float(benchmark_row.get("ret_63"), float("nan"))
    if pd.isna(symbol_ret) or pd.isna(benchmark_ret):
        return float("nan")
    return symbol_ret - benchmark_ret


def _ratio_from_rows(rows: list[pd.Series], predicate) -> Optional[float]:
    valid = 0
    passed = 0
    for row in rows:
        try:
            ok = predicate(row)
        except (TypeError, ValueError):
            ok = None
        if ok is None:
            continue
        valid += 1
        if ok:
            passed += 1
    if valid == 0:
        return None
    return passed / valid


def _breadth_report(rows: list[pd.Series], cfg: TradingConfig) -> dict:
    def above_sma(row: pd.Series, key: str) -> Optional[bool]:
        close = _safe_float(row.get("close"), float("nan"))
        sma = _safe_float(row.get(key), float("nan"))
        if pd.isna(close) or pd.isna(sma):
            return None
        return close > sma

    def positive_momentum(row: pd.Series) -> Optional[bool]:
        momentum = _safe_float(row.get("mom_126_21"), float("nan"))
        if pd.isna(momentum):
            return None
        return momentum > 0

    above_50 = _ratio_from_rows(rows, lambda row: above_sma(row, "sma_50"))
    above_200 = _ratio_from_rows(rows, lambda row: above_sma(row, "sma_200"))
    positive_mom = _ratio_from_rows(rows, positive_momentum)
    thresholds = {
        "above_sma_50_pct": cfg.regime.min_universe_above_sma_50,
        "above_sma_200_pct": cfg.regime.min_universe_above_sma_200,
        "positive_momentum_pct": cfg.regime.min_universe_positive_momentum,
    }
    values = {
        "above_sma_50_pct": above_50,
        "above_sma_200_pct": above_200,
        "positive_momentum_pct": positive_mom,
    }
    failures = []
    for key, threshold in thresholds.items():
        value = values[key]
        if value is None:
            failures.append(f"{key} unavailable")
        elif value < threshold:
            failures.append(f"{key} {value:.1%} below {threshold:.1%}")
    ok = not failures
    return {
        "enabled": cfg.regime.require_breadth,
        "ok": ok,
        "would_block": bool(cfg.regime.require_breadth and not ok),
        "reason": "ok" if ok else "; ".join(failures),
        "sample_size": len(rows),
        "above_sma_50_pct": _nullable_float(above_50),
        "above_sma_200_pct": _nullable_float(above_200),
        "positive_momentum_pct": _nullable_float(positive_mom),
        "minimums": thresholds,
    }


def _skip_summary(skipped: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for item in skipped:
        key = f"{item.get('stage', 'unknown')}: {item.get('reason', 'unknown')}"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])))


def _decision_table(candidates: list[dict], selected: list[dict], orders_preview: list[dict], limit: int = 12) -> list[dict]:
    selected_symbols = {item["symbol"] for item in selected}
    orders_by_symbol = {
        item["symbol"]: item
        for item in orders_preview
        if item.get("symbol") and item.get("direction") != "exit"
    }
    rows = []
    for item in candidates[:limit]:
        symbol = item["symbol"]
        order_preview = orders_by_symbol.get(symbol)
        report = _decision_report_item(item)
        action = "candidate"
        if symbol in selected_symbols:
            action = "selected_blocked"
        if order_preview is not None:
            action = "order"
        order = order_preview.get("order", {}) if order_preview else {}
        rows.append(
            {
                "symbol": symbol,
                "action": action,
                "direction": report["direction"],
                "decision_score": report["decision_score"],
                "signal_score": report["signal_score"],
                "trend_quality": report["trend_quality"],
                "relative_strength_63": report["relative_strength_63"],
                "sector_benchmark": report["sector_benchmark"],
                "sector_relative_strength_63": report["sector_relative_strength_63"],
                "momentum_126_21": report["momentum_126_21"],
                "rvol_20": report["rvol_20"],
                "spread_pct": report["spread_pct"],
                "last_price": report["last_price"],
                "target_notional": order_preview.get("target_notional") if order_preview else None,
                "intended_notional": order_preview.get("intended_notional") if order_preview else None,
                "qty": order.get("qty"),
                "order_type": order.get("order_type"),
            }
        )
    return rows


def _latest_feature_row_for_symbol(broker, symbol: str, cfg: TradingConfig) -> Optional[pd.Series]:
    frame = fetch_symbol_frame(broker, symbol, cfg)
    if frame is None or frame.empty:
        return None
    features = build_feature_frame(frame)
    features = features.assign(close=frame["close"], volume=frame.get("volume"))
    return _latest_row(features)


def _write_decision_log(summary: dict, cfg: TradingConfig) -> None:
    if not cfg.run.decision_log_enabled:
        return
    try:
        log_dir = Path(cfg.run.decision_log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        status = str(summary.get("status", "unknown")).replace("/", "_")
        path = log_dir / f"{summary.get('date', 'unknown')}_{status}_{stamp}.json"
        path.write_text(json.dumps(summary, indent=2, default=str))
    except Exception as exc:
        log.debug("decision log write failed: %s", exc)


def _write_strategy_outputs(summary: dict, cfg: TradingConfig) -> None:
    _write_decision_log(summary, cfg)
    try:
        ExecutionJournal(cfg.paths.execution_journal_db_path).log_summary(summary)
    except Exception as exc:
        log.debug("execution journal write failed: %s", exc)


def _freshness_report(cfg: TradingConfig, regime=None, guardian=None, now: Optional[datetime] = None) -> dict:
    """Expected vs observed timestamps for the decision log.

    Lets anyone reading the log tell why a run proceeded or stopped:
    which session the exchange had completed by the decision time, which
    bar the benchmark frame actually ended on, which sessions are
    missing, and how old the newest quote the feed supplied is.
    The expected session is a property of the calendar + decision time,
    so it is shown even when the data fetch failed entirely.
    """
    from market_calendar import latest_completed_session, session_close

    moment = now or datetime.now(timezone.utc)
    expected = latest_completed_session(moment, cfg.run.timezone)
    report = {
        "decision_time": cfg.run.decision_time,
        "timezone": cfg.run.timezone,
        "evaluated_at": moment.isoformat(),
        "expected_latest_session": expected.isoformat(),
        "expected_session_close": session_close(expected, cfg.run.timezone).isoformat(),
        "observed_latest_bar": None,
        "missing_sessions": None,
    }
    if regime is not None:
        details = getattr(regime, "details", {}) or {}
        report["observed_latest_bar"] = details.get("observed_latest_bar")
        report["missing_sessions"] = details.get("missing_sessions")
    if guardian is not None:
        last = guardian.last_data_ts()
        report["last_quote_ts"] = (
            datetime.fromtimestamp(last, tz=timezone.utc).isoformat()
            if last is not None
            else None
        )
        report["quote_age_seconds"] = round(time.time() - last, 1) if last is not None else None
    return report


def run_daily(
    broker,
    guardian,
    memory: TradeMemory,
    cfg: TradingConfig,
    dry_run: bool = False,
    order_manager: Optional[OrderManager] = None,
) -> dict:
    cfg.ensure_paths()
    today = datetime.now(timezone.utc).date().isoformat()
    state = StrategyState.load(cfg.paths.strategy_state_path)
    state.start_day(today)

    guardian.start_day()
    safety_ok = guardian.can_trade()
    if not safety_ok and not broker.live.floor_tripped():
        summary = {
            "date": today,
            "status": "halted",
            "reason": guardian.state.halt_reason,
            "data_freshness": _freshness_report(cfg, guardian=guardian),
            "orders": [],
        }
        _write_strategy_outputs(summary, cfg)
        state.last_run_ts = datetime.now(timezone.utc).isoformat()
        state.save(cfg.paths.strategy_state_path)
        return summary

    market_open = broker.is_market_open() if hasattr(broker, "is_market_open") else True
    if not dry_run and not market_open:
        summary = {
            "date": today,
            "status": "market_closed",
            "reason": "regular market is closed; real submissions are blocked",
            "data_freshness": _freshness_report(cfg, guardian=guardian),
            "orders": [],
        }
        _write_strategy_outputs(summary, cfg)
        state.last_run_ts = datetime.now(timezone.utc).isoformat()
        state.save(cfg.paths.strategy_state_path)
        return summary

    regime = market_regime(broker, cfg)
    if not regime.ok:
        summary = {
            "date": today,
            "status": "regime_blocked",
            "reason": regime.reason,
            "regime": regime.__dict__,
            "data_freshness": _freshness_report(cfg, regime=regime, guardian=guardian),
            "orders": [],
        }
        _write_strategy_outputs(summary, cfg)
        state.last_run_ts = datetime.now(timezone.utc).isoformat()
        state.save(cfg.paths.strategy_state_path)
        return summary

    # B13: exact-once order lifecycle for the live loop. The manager is
    # created only on a run that can actually submit (not dry-run, not
    # regime-blocked): dry-run never touches the intent store, so the
    # default daily invocation behaves exactly as before. When an
    # OrderManager is injected (tests), the caller owns its lifecycle;
    # the manager created here is left for refcount/GC + process exit
    # to close — a lingering WAL file is recovered by the next opener,
    # the same crash path B3 already tolerates.
    manager = order_manager
    if not dry_run and manager is None:
        manager = OrderManager(cfg.paths.order_store_path)
    # B16: startup state feedback — cancelled/expired orders since
    # submit are mirrored into the strategy state, so a routine DAY
    # expiry does not trip the end-of-run drift halt.
    state_sync: list[dict] = []
    if manager is not None:
        # Startup reconcile (Loop 5): recover any PENDING / expired-
        # CLAIMED / UNKNOWN intent BEFORE any new submission.
        # Lookup-only — reconcile never submits.
        lookup_fn = getattr(broker.paper, "get_by_client_order_id", None)
        if lookup_fn is None:
            log.warning(
                "paper leg has no get_by_client_order_id; startup reconcile "
                "skipped (lookups cannot adopt unknown intents)"
            )
        else:
            manager.reconcile_all_unknowns(
                lookup_fn=lookup_fn,
                grace_seconds=0.0,
                max_polls=3,
                sleep_fn=lambda s: None,
            )
            # A confirmed-accepted order may have been cancelled or
            # expired by the broker since submit; reconcile() never
            # re-checks SUBMITTED intents, so sync them explicitly.
            manager.reconcile_open_intents(lookup_fn=lookup_fn)
            # Dry-run is a preview and must not mutate strategy state.
            if not dry_run:
                state_sync = _mirror_cancelled_intents_into_state(
                    state, manager.cancelled_intents()
                )
                for adj in state_sync:
                    log.info("startup state sync: %s", adj)

    benchmark_row = None
    try:
        benchmark_frame = fetch_symbol_frame(broker, cfg.regime.benchmark_symbol, cfg)
    except StaleBarsError:
        # The regime gate already validated this frame; if it is stale
        # now, proceed without a benchmark row rather than crashing.
        benchmark_frame = None
    if benchmark_frame is not None and not benchmark_frame.empty:
        benchmark_features = build_feature_frame(benchmark_frame)
        benchmark_features = benchmark_features.assign(close=benchmark_frame["close"])
        benchmark_row = _latest_row(benchmark_features)

    universe = select_universe(broker, cfg)
    sector_rows = {}
    if cfg.sector.enabled:
        for sector_symbol in sector_benchmarks_for(tuple(universe)):
            row = _latest_feature_row_for_symbol(broker, sector_symbol, cfg)
            if row is not None:
                sector_rows[sector_symbol] = row
    candidates = []
    orders_preview = []
    skipped = []
    breadth_rows = []

    for symbol in universe:
        if symbol in state.submitted_today:
            skipped.append({"symbol": symbol, "stage": "dedupe", "reason": "already submitted today"})
            continue
        try:
            frame = fetch_symbol_frame(broker, symbol, cfg)
        except StaleBarsError as exc:
            skipped.append({
                "symbol": symbol,
                "stage": "data",
                "reason": "market data stale",
                "expected_latest_session": exc.details["expected_latest_session"],
                "observed_latest_bar": exc.details["observed_latest_bar"],
                "missing_sessions": exc.details["missing_sessions"],
            })
            continue
        if frame is None or frame.empty:
            skipped.append({"symbol": symbol, "stage": "data", "reason": "market data unavailable"})
            continue
        features = build_feature_frame(frame)
        features = features.assign(close=frame["close"], volume=frame["volume"])
        row = _latest_row(features)
        if row is None or pd.isna(row.get("close", float("nan"))):
            skipped.append({"symbol": symbol, "stage": "indicators", "reason": "latest indicators unavailable"})
            continue
        breadth_rows.append(row)

        direction_series = composite_signal(features).dropna()
        score_series = composite_score(features).dropna()
        quality_series = trend_quality_score(features).dropna()
        if direction_series.empty or score_series.empty or quality_series.empty:
            skipped.append({"symbol": symbol, "stage": "signal", "reason": "signal unavailable"})
            continue
        direction = direction_series.iloc[-1]
        score = score_series.iloc[-1]
        quality = quality_series.iloc[-1]

        if (
            pd.isna(row.get("rsi_14", float("nan")))
            or pd.isna(row.get("mfi_14", float("nan")))
            or pd.isna(row.get("mom_126_21", float("nan")))
        ):
            skipped.append({"symbol": symbol, "stage": "indicators", "reason": "required indicators unavailable"})
            continue
        if float(row.get("mom_126_21", 0.0)) < cfg.signals.min_momentum:
            skipped.append({"symbol": symbol, "stage": "signal", "reason": "momentum below minimum"})
            continue
        relative_strength = _relative_strength_63(row, benchmark_row)
        if cfg.signals.min_relative_strength_63 > -9:
            if pd.isna(relative_strength) or relative_strength < cfg.signals.min_relative_strength_63:
                reason = (
                    f"63-day relative strength "
                    f"{relative_strength * 100 if not pd.isna(relative_strength) else float('nan'):.2f}% "
                    f"below {cfg.signals.min_relative_strength_63 * 100:.2f}%"
                )
                skipped.append({"symbol": symbol, "stage": "signal", "reason": reason})
                log.info("skipping %s: %s", symbol, reason)
                continue
        sector_benchmark = sector_benchmark_for(symbol) if cfg.sector.enabled else None
        sector_relative_strength = _relative_strength_63(row, sector_rows.get(sector_benchmark))
        if sector_benchmark and cfg.sector.min_relative_strength_63 > -9:
            if pd.isna(sector_relative_strength) or sector_relative_strength < cfg.sector.min_relative_strength_63:
                reason = (
                    f"63-day sector relative strength "
                    f"{sector_relative_strength * 100 if not pd.isna(sector_relative_strength) else float('nan'):.2f}% "
                    f"vs {sector_benchmark} below {cfg.sector.min_relative_strength_63 * 100:.2f}%"
                )
                skipped.append({"symbol": symbol, "stage": "signal", "reason": reason})
                log.info("skipping %s: %s", symbol, reason)
                continue
        if float(quality) < cfg.signals.min_trend_quality:
            skipped.append({"symbol": symbol, "stage": "signal", "reason": "trend quality below minimum"})
            continue
        quote = _quote_with_spread_pct(broker.paper.latest_quote(symbol) or broker.live.latest_quote(symbol))
        if quote is None:
            skipped.append({"symbol": symbol, "stage": "risk", "reason": "valid quote unavailable"})
            continue
        snap = _decision_snapshot(symbol, direction, row, quote)
        ok_downside, downside_reason = downside_ok(row, quote, cfg)
        if not ok_downside:
            skipped.append({"symbol": symbol, "stage": "risk", "reason": downside_reason, "quote": quote})
            log.info("skipping %s: %s", symbol, downside_reason)
            continue
        warning = memory.flag_if_repeating_loss(snap)
        if warning:
            skipped.append({"symbol": symbol, "stage": "memory", "reason": warning})
            log.info(warning)
            continue

        if direction == "no_trade":
            skipped.append({"symbol": symbol, "stage": "signal", "reason": "no trade signal"})
            continue

        last_price = _entry_price(quote, row)
        rank = _decision_rank(
            row,
            quote,
            float(score),
            float(quality),
            relative_strength,
            sector_relative_strength,
            cfg,
        )
        if rank["decision_score"] < cfg.signals.min_decision_score:
            reason = (
                f"decision score {rank['decision_score']:.2f} "
                f"below minimum {cfg.signals.min_decision_score:.2f}"
            )
            skipped.append({"symbol": symbol, "stage": "signal", "reason": reason})
            log.info("skipping %s: %s", symbol, reason)
            continue
        candidates.append(
            {
                "symbol": symbol,
                "direction": direction,
                "score": float(score),
                "trend_quality": float(quality),
                "decision_score": rank["decision_score"],
                "rank": rank,
                "relative_strength_63": relative_strength,
                "sector_benchmark": sector_benchmark,
                "sector_relative_strength_63": sector_relative_strength,
                "row": row,
                "quote": quote,
                "last_price": last_price,
                "snapshot": snap,
            }
        )

    candidates.sort(key=lambda item: (item["decision_score"], item["score"], item["trend_quality"]), reverse=True)
    breadth = _breadth_report(breadth_rows, cfg)
    if not dry_run and cfg.regime.require_breadth and not breadth["ok"]:
        summary = {
            "date": today,
            "status": "breadth_blocked",
            "reason": breadth["reason"],
            "regime": regime.__dict__,
            "data_freshness": _freshness_report(cfg, regime=regime, guardian=guardian),
            "breadth": breadth,
            "candidates": [_decision_report_item(item) for item in candidates],
            "decision_report": [_decision_report_item(item) for item in candidates],
            "decision_table": _decision_table(candidates, [], []),
            "skip_summary": _skip_summary(skipped),
            "selected": [],
            "orders": [],
            "skipped": skipped,
        }
        _write_strategy_outputs(summary, cfg)
        state.last_run_ts = datetime.now(timezone.utc).isoformat()
        state.save(cfg.paths.strategy_state_path)
        return summary
    selected = candidates[: cfg.sizing.target_n_positions]

    paper_equity = broker.paper.equity()
    live_equity = broker.live.equity()
    account_equity = paper_equity or live_equity or cfg.paper.starting_capital
    equity = _strategy_equity(cfg, account_equity)
    paper_positions = broker.paper.positions()
    live_positions = broker.live.positions()
    strategy_symbols = _strategy_owned_symbols(state)
    existing_strategy_notional = _positions_notional(paper_positions, strategy_symbols)
    # B15: reserve budget for open buy orders (paper leg). A filled
    # position is already in existing_strategy_notional at market
    # notional; an open buy's unfilled quantity can still fill, so it
    # reserves remaining * reservation price. Without this, a second
    # buy is sized as if the first order's capital were still free.
    # Same data source as reconcile() (_open_orders_for_leg), so the
    # two views of "what the broker holds" cannot disagree. Sells
    # never reserve; non-strategy symbols are ignored. The reservation
    # is part of existing_strategy_notional from here on, so the
    # decision log's existing_notional is the honest committed number.
    last_prices: dict[str, float] = {}
    for _item in candidates:
        try:
            _price = float(_item.get("last_price"))
        except (TypeError, ValueError):
            continue
        if _price > 0:
            last_prices[str(_item["symbol"])] = _price
    entry_prices: dict[str, float] = {}
    for _sym, _rec in state.open_entries.items():
        try:
            _price = float(_rec.get("entry_price"))
        except (TypeError, ValueError):
            continue
        if _price > 0:
            entry_prices[str(_sym)] = _price
    reservation_total, reservation_detail = _open_buy_reservations(
        _open_orders_for_leg(broker.paper), strategy_symbols,
        last_prices, entry_prices)
    if reservation_detail:
        log.info("open buy reservations: %s (total %.2f)",
                 reservation_detail, reservation_total)
    existing_strategy_notional += reservation_total
    max_deployed = equity * cfg.sizing.max_deployed_pct
    remaining_budget = max(0.0, max_deployed - existing_strategy_notional)
    blocked_symbols = set(state.open_entries) | set(paper_positions) | set(live_positions)
    eligible_new_count = len([item for item in selected if item["symbol"] not in blocked_symbols])
    target_notional = remaining_budget / max(eligible_new_count, 1)

    for item in selected:
        symbol = item["symbol"]
        quote = item["quote"]
        direction = item["direction"]
        row = item["row"]
        snap = item["snapshot"]
        if symbol in state.open_entries:
            skipped.append({"symbol": symbol, "stage": "sizing", "reason": "already open"})
            continue
        if symbol in paper_positions or symbol in live_positions:
            skipped.append(
                {
                    "symbol": symbol,
                    "stage": "sizing",
                    "reason": "untracked existing broker position; not mixing strategy and manual lots",
                }
            )
            continue

        price_for_sizing = item["last_price"] or float(row["close"])
        capped_notional = min(target_notional, equity * cfg.sizing.max_position_pct)
        vol_scale = volatility_scale(float(row.get("rvol_20", float("nan"))), cfg)
        capped_notional *= vol_scale
        qty = max(0, math.floor(capped_notional / max(price_for_sizing, 0.01)))
        intended_notional = qty * price_for_sizing
        if qty <= 0:
            skipped.append(
                {
                    "symbol": symbol,
                    "stage": "sizing",
                    "reason": "calculated whole-share quantity is zero",
                    "price": round(price_for_sizing, 2),
                    "target_notional": round(capped_notional, 2),
                }
            )
            continue

        side = _side_for_signal(direction)
        order = _build_order(symbol, side, qty, quote, direction, cfg)
        paper_ok, paper_reason = guardian.validate_order(
            order,
            last_price=price_for_sizing,
            equity=equity,
            positions=_position_payload_for_validation(paper_positions, strategy_symbols | {symbol}),
        )
        live_ok, live_reason = guardian.validate_order(
            order,
            last_price=price_for_sizing,
            equity=equity,
            positions=_position_payload_for_validation(live_positions, strategy_symbols | {symbol}),
        )
        if not paper_ok or not live_ok:
            skipped.append(
                {
                    "symbol": symbol,
                    "stage": "guardian",
                    "reason": f"paper={paper_reason}; live={live_reason}",
                    "intended_notional": round(intended_notional, 2),
                }
            )
            log.info("order rejected for %s: paper=%s live=%s", symbol, paper_reason, live_reason)
            continue

        paper_fill = _entry_price_from_result(None, price_for_sizing, quote, side)
        live_fill = _entry_price_from_result(None, price_for_sizing, quote, side)

        if dry_run:
            trail_percent = _trail_percent_for_row(row, cfg)
            orders_preview.append(
                {
                    "symbol": symbol,
                    "direction": direction,
                    "decision_score": item["decision_score"],
                    "score": item["score"],
                    "trend_quality": item["trend_quality"],
                    "relative_strength_63": _nullable_float(item["relative_strength_63"]),
                    "last_price": round(price_for_sizing, 2),
                    "target_notional": round(capped_notional, 2),
                    "intended_notional": round(intended_notional, 2),
                    "volatility_scale": vol_scale,
                    "trail_percent": trail_percent,
                    "order": order,
                    "dry_run": True,
                }
            )
            continue

        paper_result = None
        live_result = None
        if manager is not None and manager.is_blocked(symbol, account="paper"):
            # An earlier intent for this symbol is UNKNOWN (its lookup
            # stayed inconclusive): refuse the conflicting order
            # outright — no submission, no decision, no open entry.
            # The strategy re-derives the same order next run; once
            # the blocking intent resolves, that run proceeds normally.
            # (OrderManager.submit would also refuse it as BLOCKED;
            # pre-checking here keeps the refusal out of the guardian
            # accounting and the decision record entirely.)
            skipped.append({
                "symbol": symbol,
                "stage": "order_manager",
                "reason": f"blocked by unresolved intent for {symbol} (paper); no submission made",
            })
            log.info("skipping %s: blocked by unresolved intent", symbol)
            continue
        if manager is not None:
            intent = _live_intent(
                order,
                {
                    "symbol": symbol,
                    "direction": direction,
                    "decision_score": round(float(item["decision_score"]), 6),
                    "score": round(float(item["score"]), 6),
                    "trend_quality": round(float(item["trend_quality"]), 6),
                    # 2dp, matching the orders_preview last_price: the
                    # intent's inputs_hash must be reproducible from
                    # the decision log (see _live_intent docstring).
                    "last_price": round(float(price_for_sizing), 2),
                    "target_notional": round(float(capped_notional), 2),
                    "intended_notional": round(float(intended_notional), 2),
                },
                today,
            )
            paper_result = guardian.submit(
                order,
                lambda o, _intent=intent: _submit_paper_via_manager(o, _intent, manager, broker),
            )
        else:
            intent = None
            paper_result = guardian.submit(order, broker.paper.submit)
        if not broker.live.floor_tripped():
            live_result = guardian.submit(order, broker.live.submit)

        if paper_result is not None:
            paper_fill = _entry_price_from_result(paper_result, price_for_sizing, quote, side)
        if live_result is not None:
            live_fill = _entry_price_from_result(live_result, price_for_sizing, quote, side)

        decision_id = memory.log_decision(
            snap,
            order_type=order["order_type"],
            intended_price=price_for_sizing,
            qty=qty,
            paper_fill=paper_fill,
            live_fill=live_fill,
        )
        if side == "buy" and live_result is not None:
            lot_id = memory.open_tax_lot(symbol, qty, live_fill * qty)
        else:
            lot_id = None

        state.submitted_today.append(symbol)
        state.open_entries[symbol] = {
            "symbol": symbol,
            "qty": qty,
            "side": side,
            "direction": direction,
            "decision_id": decision_id,
            "lot_id": lot_id,
            "entry_price": live_fill if live_fill else paper_fill,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            # B16: the intent behind this entry, so the startup sync
            # can mirror a cancelled/expired order back into the
            # state (and the exit can confirm the working order is
            # gone). None when no manager was used for the entry.
            "intent_id": intent["intent_id"] if intent is not None else None,
        }
        state.expected_positions["paper"][symbol] = {"qty": qty, "decision_id": decision_id}
        if live_result is not None and not broker.live.floor_tripped():
            state.expected_positions["live"][symbol] = {"qty": qty, "decision_id": decision_id}

        trail_percent = _trail_percent_for_row(row, cfg)
        if side == "buy" and paper_result is not None and not dry_run:
            broker.paper.attach_trailing_stop(symbol, qty, trail_percent)
        if side == "buy" and live_result is not None and not dry_run:
            broker.live.attach_trailing_stop(symbol, qty, trail_percent)

        orders_preview.append(
            {
                "symbol": symbol,
                "direction": direction,
                "decision_score": item["decision_score"],
                "score": item["score"],
                "trend_quality": item["trend_quality"],
                "relative_strength_63": _nullable_float(item["relative_strength_63"]),
                "last_price": round(price_for_sizing, 2),
                "target_notional": round(capped_notional, 2),
                "intended_notional": round(intended_notional, 2),
                "volatility_scale": vol_scale,
                "trail_percent": trail_percent,
                "order": order,
                "decision_id": decision_id,
                "dry_run": dry_run,
            }
        )

    for symbol, record in list(state.open_entries.items()):
        if not cfg.exits.exit_on_signal_loss:
            continue
        current_signal = next((item for item in candidates if item["symbol"] == symbol), None)
        if current_signal is not None and current_signal["direction"] != "no_trade":
            continue
        quote = _quote_with_spread_pct(broker.paper.latest_quote(symbol) or broker.live.latest_quote(symbol))
        exit_side = "sell" if record.get("side") == "buy" else "buy"
        entry_qty = record.get("qty", 0)
        if entry_qty <= 0:
            continue
        # B16: cancel the working entry order FIRST and size the exit
        # to what the entry actually holds. A buy that only partially
        # filled must not be sold at the full entry qty, and a working
        # buy that fills after the exit would re-open the position.
        # If the cancel fails or cannot be confirmed, do NOT submit
        # the exit — record it as skipped and leave the entry open
        # for the next run (fail-closed). The dry-run preview keeps
        # the entry qty: the filled qty is only known when the exit
        # runs, after the cancel.
        exit_qty = entry_qty
        if not dry_run:
            ok, exit_qty, cancel_reason = _cancel_entry_orders_for_exit(
                broker, symbol, record.get("side", "buy"),
                manager=manager,
                intent_id=record.get("intent_id"),
            )
            if not ok:
                skipped.append({
                    "symbol": symbol,
                    "stage": "exit_cancel",
                    "reason": cancel_reason,
                })
                log.info("skipping exit for %s: %s", symbol, cancel_reason)
                continue
        exit_order = _build_order(symbol, exit_side, exit_qty, quote, "mild_up", cfg)
        if dry_run:
            orders_preview.append(
                {
                    "symbol": symbol,
                    "direction": "exit",
                    "order": exit_order,
                    "dry_run": True,
                }
            )
            continue
        if exit_qty <= 0:
            # Nothing held: the entry never filled (its buy was
            # cancelled/expired before the exit). No sell to submit —
            # just close the entry record below.
            log.info(
                "exit for %s: nothing held (entry qty %s, filled 0); "
                "closing entry without a sell", symbol, entry_qty,
            )
        elif record["side"] == "buy":
            if manager is not None and manager.is_blocked(symbol, account="paper"):
                # Do not exit into an unresolved intent for this symbol;
                # leave the entry open and retry next run.
                log.info("skipping exit for %s: blocked by unresolved intent", symbol)
                continue
            if manager is not None:
                exit_intent = _live_intent(
                    exit_order,
                    {
                        "symbol": symbol,
                        "side": exit_side,
                        "qty": float(exit_qty),
                        "exit_reason": "signal_loss",
                    },
                    today,
                )
                guardian.submit(
                    exit_order,
                    lambda o, _intent=exit_intent: _submit_paper_via_manager(o, _intent, manager, broker),
                )
            else:
                guardian.submit(exit_order, broker.paper.submit)
            if not broker.live.floor_tripped():
                guardian.submit(exit_order, broker.live.submit)
        exit_price = _close_price_from_quote(quote, exit_side)
        holding_days = (
            datetime.now(timezone.utc) - datetime.fromisoformat(record["opened_at"])
        ).total_seconds() / 86400.0
        realized_pl = (exit_price - float(record["entry_price"])) * exit_qty
        memory.close_decision(record["decision_id"], exit_price, realized_pl, holding_days)
        if record.get("lot_id"):
            memory.close_tax_lot(record["lot_id"], exit_price * exit_qty)
        state.open_entries.pop(symbol, None)
        state.expected_positions["paper"].pop(symbol, None)
        state.expected_positions["live"].pop(symbol, None)

    if dry_run:
        summary = {
            "date": today,
            "status": "dry_run",
            "regime": regime.__dict__,
            "data_freshness": _freshness_report(cfg, regime=regime, guardian=guardian),
            "breadth": breadth,
            "budget": {
                "strategy_capital": cfg.sizing.strategy_capital,
                "account_equity": account_equity,
                "sizing_equity": equity,
                "max_deployed": round(max_deployed, 2),
                "existing_notional": round(existing_strategy_notional, 2),
                "open_buy_reservations": round(reservation_total, 2),
                "buy_reservations": reservation_detail,
                "remaining_budget": round(remaining_budget, 2),
                "target_positions": cfg.sizing.target_n_positions,
                "max_position_pct": cfg.sizing.max_position_pct,
            },
            "candidates": [
                _decision_report_item(item) for item in candidates
            ],
            "decision_report": [_decision_report_item(item) for item in candidates],
            "decision_table": _decision_table(candidates, selected, orders_preview),
            "skip_summary": _skip_summary(skipped),
            "selected": [item["symbol"] for item in selected],
            "orders": orders_preview,
            "skipped": skipped,
            "state_sync": state_sync,
        }
        _write_strategy_outputs(summary, cfg)
        return summary

    state.last_run_ts = datetime.now(timezone.utc).isoformat()
    state.save(cfg.paths.strategy_state_path)
    broker.save_state()

    reconcile_result = reconcile(broker, memory, cfg, guardian=guardian)
    summary = memory.daily_summary()
    status = broker.status()
    title, body = format_daily(summary, status)
    Notifier(cfg).send(title, body)

    summary = {
        "date": today,
        "status": "ok" if reconcile_result["ok"] else "needs_attention",
        "regime": regime.__dict__,
        "data_freshness": _freshness_report(cfg, regime=regime, guardian=guardian),
        "breadth": breadth,
        "budget": {
            "strategy_capital": cfg.sizing.strategy_capital,
            "account_equity": account_equity,
            "sizing_equity": equity,
            "max_deployed": round(max_deployed, 2),
            "existing_notional": round(existing_strategy_notional, 2),
            "open_buy_reservations": round(reservation_total, 2),
            "buy_reservations": reservation_detail,
            "remaining_budget": round(remaining_budget, 2),
        },
        "decision_report": [_decision_report_item(item) for item in candidates],
        "decision_table": _decision_table(candidates, selected, orders_preview),
        "skip_summary": _skip_summary(skipped),
        "selected": [item["symbol"] for item in selected],
        "orders": orders_preview,
        "skipped": skipped,
        "state_sync": state_sync,
        "summary": summary,
        "broker": status,
        "reconcile": reconcile_result,
    }
    _write_strategy_outputs(summary, cfg)
    return summary
