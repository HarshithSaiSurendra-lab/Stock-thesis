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
from reconcile import _open_orders_for_leg, _open_orders_including_done_for_day, reconcile
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
    # B17.2 / Detail 2: exits that were ACCEPTED but have not filled
    # yet (the exit order is working on the broker). One record per
    # symbol, persisted in the state file — the exposure record is
    # DURABLE, not in-memory only, so a fresh process / next run reads
    # the same record: {exit_intent_id, account, qty, filled_qty,
    # entry_price, decision_id, lot_id, exposure_started_at,
    # exposure_uncertain}. The exposure interval for each share ends
    # when it is sold (the exit fills) or protection is confirmed
    # restored (a new stop is attached for it). The record is removed
    # when the exit intent reaches a terminal state (FILLED /
    # CANCELLED / REJECTED) or a human resolution.
    pending_exits: dict = field(default_factory=dict)

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


def _live_intent(order: dict, inputs: dict, trading_day: str, account: str = "paper") -> dict:
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

    B17.5: `account` is part of the idempotency key (the paper and
    live legs are separate broker accounts, so the same logical order
    on both is two broker orders with two client_order_ids). Adding
    "account" changes EVERY intent id, paper included, vs pre-B17 —
    safe only because no production intent store exists yet (the soak
    has not started; zero orders have ever been submitted). Do not
    deploy mid-soak with open intents.
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
        account=account,
    )


def _live_leg_enabled(broker) -> bool:
    """Is the live leg enabled (B17.5)? The real AlpacaLeg exposes
    cfg.enabled; the fake leg exposes .enabled. A disabled live leg
    must NEVER get an intent (submitting to it returns None, which
    would wrongly become UNKNOWN) — the live entry/exit stay the
    direct no-op calls they are today."""
    live = getattr(broker, "live", None)
    if live is None:
        return False
    cfg = getattr(live, "cfg", None)
    if cfg is not None and getattr(cfg, "enabled", None) is not None:
        return bool(cfg.enabled)
    return bool(getattr(live, "enabled", False))


def _submit_live_via_manager(order: dict, intent: dict, manager: OrderManager, broker) -> Optional[dict]:
    """place_order_fn for guardian.submit: route the LIVE leg through
    the B3 OrderManager (account="live"), inside the guardian's gate
    and breaker — the same exact-once protection the paper leg has
    (B17.5). Called ONLY when the live leg is enabled; while it is
    disabled the caller uses the direct broker.live.submit no-op
    instead, and no intent is ever created for a disabled leg.
    Returns the broker-shaped result dict on success, None when no
    broker order exists (REJECTED / BLOCKED), and raises TimeoutError
    when the outcome is UNKNOWN — exactly the paper leg's path.
    """
    def _place(submitted: dict) -> dict:
        result = broker.live.submit(submitted)
        if result is None:
            raise RuntimeError(
                "live leg rejected the order without placing it "
                "(disabled, floor, or position cap)"
            )
        return result

    lookup_fn = getattr(broker.live, "get_by_client_order_id", None)
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
            f"live order outcome unknown for {order['symbol']}: intent "
            f"{intent['intent_id']} UNKNOWN after lookup; alert recorded, "
            "symbol blocked"
        )
    # REJECTED / BLOCKED: no broker order exists. Return None — the same
    # as today's None-return path (guardian records no error, the loop
    # treats it as no fill).
    return None


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
    and halts on drift rather than silently guessing. A DONE_FOR_DAY
    intent is NEVER mirrored here (B16 correction): the order is
    still working, so its expected position and its budget
    reservation are preserved — only a CONFIRMED expired/canceled
    (which adopts as CANCELLED) releases the unfilled remainder.
    Returns one adjustment dict per entry changed, for the run summary.
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


def _adopt_cancel_result(manager, intent, broker_order_id, result):
    """Adopt a cancel call's confirmed status into the intent store.

    Only the entry intent's own order is adopted (matched by broker
    order id): a cancel result for another order must never touch
    this intent's record. A dict result with a terminal status
    (cancelled / canceled / expired / filled / rejected) is adopted
    — it is the broker's final view of the order. A None result is
    NOT adopted here: the broker having no such order is only
    confirmation when the per-order lookup agrees (see the tripwire
    in _cancel_working_orders_on_leg); adopting from a lone None
    would let a leg that cannot find the order clear a working one.
    """
    if manager is None or intent is None or not intent.get("intent_id"):
        return
    if str(broker_order_id or "") != str(intent.get("broker_order_id") or ""):
        return
    if not isinstance(result, dict):
        return
    status = str(result.get("status") or "").lower()
    if status in {"cancelled", "canceled", "expired", "filled", "rejected"}:
        manager.adopt_lookup(intent["intent_id"], result)


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

    B16 correction: the entry's order may be INVISIBLE to
    open_orders() — a DONE_FOR_DAY order is excluded from the
    broker's OPEN query — but it is still working and can still fill
    tomorrow. When the entry's intent is non-terminal (SUBMITTED /
    PARTIALLY_FILLED / DONE_FOR_DAY), its order is cancelled by
    client_order_id explicitly, even when it is not in the visible
    list. And the tripwire's acceptable set no longer includes
    done_for_day: a lookup that returns done_for_day means the order
    is still working, so it VETOES (B16 wrongly treated it as
    confirmation the order was gone). A terminal lookup result is
    adopted into the intent store promptly, so a later block check
    in the same run sees the truth instead of a stale DONE_FOR_DAY.
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
    cancel = getattr(leg, "cancel_order", None)
    # The entry intent, when non-terminal: its order may be invisible
    # to open_orders() (the DONE_FOR_DAY case) but it is still working.
    intent = None
    if manager is not None and intent_id:
        intent = manager.get_intent(intent_id)
    intent_live = (intent is not None
                   and intent.get("status") in {"SUBMITTED", "PARTIALLY_FILLED", "DONE_FOR_DAY"}
                   and intent.get("broker_order_id"))
    if (working or intent_live) and cancel is None:
        return False, 0.0, "leg has no cancel_order; cannot cancel the entry order"
    filled = 0.0
    cancelled_broker_ids = set()
    for order in working:
        filled += float(order.get("filled_qty") or 0.0)
        try:
            result = cancel(order_id=order.get("id"),
                            client_order_id=order.get("client_order_id"))
        except Exception as exc:  # noqa: BLE001 - fail-closed on any cancel failure
            return False, 0.0, f"cancel failed for {order.get('id')}: {exc}"
        if isinstance(result, dict) and result.get("filled_qty") is not None:
            # The cancel result is fresher than the pre-cancel snapshot.
            filled += float(result.get("filled_qty") or 0.0) - float(order.get("filled_qty") or 0.0)
        if isinstance(result, dict):
            status = str(result.get("status") or "").lower()
            if status not in {"cancelled", "canceled", "expired", "filled", "rejected"}:
                # The cancel did not take effect (the order is still
                # working) — the re-fetch below would catch a visible
                # order, but a done_for_day order is invisible to it,
                # so veto here rather than trusting the re-fetch.
                return False, 0.0, f"cancel did not take effect for {order.get('id')}: still {status}"
        cancelled_broker_ids.add(str(order.get("id")))
        _adopt_cancel_result(manager, intent, order.get("id"), result)
    cancel_gone = False  # the intent's order could not be found to cancel
    if intent_live and str(intent.get("broker_order_id")) not in cancelled_broker_ids:
        # Not visible in open_orders (the DONE_FOR_DAY case): cancel
        # by client_order_id (== intent_id).
        try:
            result = cancel(client_order_id=intent_id)
        except Exception as exc:  # noqa: BLE001 - fail-closed on any cancel failure
            return False, 0.0, f"cancel failed for {intent_id}: {exc}"
        if isinstance(result, dict):
            if result.get("filled_qty") is not None:
                filled += float(result.get("filled_qty") or 0.0)
            else:
                filled += float(intent.get("filled_qty") or 0.0)
            status = str(result.get("status") or "").lower()
            if status not in {"cancelled", "canceled", "expired", "filled", "rejected"}:
                return False, 0.0, f"cancel did not take effect for {intent_id}: still {status}"
        else:
            # None: the broker has no such order (404, already gone).
            # Nothing remains to fill; the tripwire below decides
            # whether that is confirmation (lookup also None) or a
            # veto (lookup says it is still working).
            cancel_gone = True
            filled += float(intent.get("filled_qty") or 0.0)
        _adopt_cancel_result(manager, intent, intent.get("broker_order_id"), result)
    if working or intent_live:
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
        if filled == 0.0:
            # No visible working order was cancelled and the intent
            # reported no fills: the entry's fills may be visible
            # only as a position (a fill landed after the last
            # reconcile). Fall back to the leg's position.
            try:
                positions = leg.positions()
            except Exception as exc:  # noqa: BLE001 - fail-closed
                return False, 0.0, f"positions fetch failed: {exc}"
            filled = abs(float((positions or {}).get(symbol, {}).get("qty", 0.0) or 0.0))
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
    # order is gone — a WORKING status vetoes the exit. DONE_FOR_DAY
    # vetoes: the order is still working and can still fill tomorrow.
    if intent_live:
        if lookup_fn is None:
            return False, 0.0, "no lookup to confirm the entry order is gone"
        try:
            found = lookup_fn(intent_id)
        except Exception as exc:  # noqa: BLE001 - fail-closed; the exit DECISION is
            # re-made next run (no exit intent exists yet at this point,
            # so nothing is re-submitted — the veto only delays the
            # decision, it never re-submits an UNKNOWN intent).
            return False, 0.0, f"lookup failed for {intent_id}: {exc}"
        if found is not None:
            status = str(found.get("status") or "").lower()
            if status not in {"cancelled", "canceled", "expired", "filled", "rejected"}:
                return False, 0.0, f"entry order {intent_id} still {status} after cancel"
            if manager is not None:
                manager.adopt_lookup(intent_id, found)
        elif cancel_gone:
            # The cancel found no such order AND the lookup cannot
            # find it either: two independent "not found" answers
            # agree the order is gone — adopt CANCELLED so the store
            # does not keep blocking the symbol on a vanished order.
            if manager is not None:
                manager.adopt_lookup(intent_id, {
                    "id": intent.get("broker_order_id"),
                    "status": "cancelled",
                    "filled_qty": intent.get("filled_qty") or 0.0,
                })
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


def _cancel_trailing_stop_on_leg(leg, symbol, *, trail_stop_id=None, trail_stop_coid=None,
                                 lookup_fn=None):
    """Cancel the open trailing-stop sell(s) for `symbol` on one leg.

    Returns (ok, reason, fills). ok True also when there is no
    trailing stop to cancel — but ONLY when no stop is recorded for
    the entry (no trail_stop_id/coid) or the recorded stop is
    CONFIRMED terminal BY ORDER ID. Detail 1: a stop's final state
    is never inferred from its absence in the open-orders list.
    When a stop is recorded and its state cannot be confirmed by
    order id (lookup None / raises / unrecognized status), ok is
    False — the exit must NOT proceed, so the stop stays in place
    protecting the shares (fail-closed).
    fills is a list of {client_order_id, id, status, filled_qty,
    filled_avg_price} for every stop confirmed (cancelled or already
    terminal) — the exit uses it for the stop-fill P&L attribution
    (B17.3) and the oversell check (B17.4). The fill price is
    refreshed BY ORDER ID when the cancel result lacks it.
    """
    if lookup_fn is None:
        lookup_fn = getattr(leg, "get_by_client_order_id", None)
    fills: list[dict] = []
    # 1. The recorded stop's state BY ORDER ID (Detail 1).
    recorded_state = None
    if trail_stop_coid is not None or trail_stop_id is not None:
        recorded_state = _stop_fill_state(
            leg, symbol=symbol, trail_stop_id=trail_stop_id,
            trail_stop_coid=trail_stop_coid, lookup_fn=lookup_fn)
        if recorded_state["ambiguous"]:
            return False, (
                "trailing-stop state unconfirmed by order-id lookup; "
                "the exit stays blocked until the stop's final state is confirmed"
            ), []
        if recorded_state["found"] and str(recorded_state["status"] or "").lower() in {
                "cancelled", "canceled", "expired", "filled", "rejected"}:
            # Already terminal: record its fills (B17.3) and do not
            # cancel it. (Any OTHER stop for the symbol is still
            # found by the open-orders scan below.)
            fills.append({
                "client_order_id": recorded_state["client_order_id"],
                "id": recorded_state["id"],
                "status": str(recorded_state["status"]).lower(),
                "filled_qty": float(recorded_state["filled_qty"] or 0.0),
                "filled_avg_price": recorded_state["filled_avg_price"],
            })
    try:
        open_orders = leg.open_orders()
    except Exception as exc:  # noqa: BLE001 - fail-closed on any fetch failure
        return False, f"open-orders fetch failed: {exc}", fills
    stops = [
        o for o in (open_orders or [])
        if str(o.get("symbol")) == symbol
        and str(o.get("order_type") or "").lower() == "trailing_stop"
        and str(o.get("side", "")).lower() == "sell"
        and float(o.get("qty") or 0.0) - float(o.get("filled_qty") or 0.0) > 0
    ]
    if trail_stop_id is not None:
        stops = [o for o in stops if str(o.get("id")) == str(trail_stop_id)] or stops
    # A working stop that is INVISIBLE to open_orders (its state was
    # confirmed by order id above) must still be cancelled.
    if (recorded_state is not None and recorded_state["found"]
            and str(recorded_state["status"] or "").lower() not in {
                "cancelled", "canceled", "expired", "filled", "rejected"}
            and not any(str(o.get("client_order_id")) == str(recorded_state["client_order_id"])
                        for o in stops)):
        stops.append({
            "id": recorded_state["id"],
            "client_order_id": recorded_state["client_order_id"],
            "symbol": symbol, "side": "sell", "order_type": "trailing_stop",
            "qty": recorded_state["qty"],
            "filled_qty": recorded_state["filled_qty"],
            "status": recorded_state["status"],
        })
    if not stops:
        # No working stop to cancel (none recorded, none visible, or
        # the recorded one was already terminal — its fills are above).
        return True, "", fills
    cancel = getattr(leg, "cancel_order", None)
    if cancel is None:
        return False, "leg has no cancel_order; cannot cancel the trailing stop", fills
    for order in stops:
        try:
            result = cancel(order_id=order.get("id"),
                            client_order_id=order.get("client_order_id"))
        except Exception as exc:  # noqa: BLE001 - fail-closed on any cancel failure
            return False, f"trailing-stop cancel failed for {order.get('id')}: {exc}", fills
        if isinstance(result, dict):
            status = str(result.get("status") or "").lower()
            if status not in {"cancelled", "canceled", "expired", "filled", "rejected"}:
                return False, (
                    f"trailing-stop cancel did not take effect for {order.get('id')}: "
                    f"still {status}"
                ), fills
            fill = {
                "client_order_id": result.get("client_order_id") or order.get("client_order_id"),
                "id": result.get("id") or order.get("id"),
                "status": status,
                "filled_qty": float(result.get("filled_qty") or 0.0),
                "filled_avg_price": _opt_float_or_none(result.get("filled_avg_price")),
            }
            # Refresh the fill price BY ORDER ID when the cancel
            # result lacks it (B17.3): the stop's filled_avg_price is
            # what the P&L attribution needs.
            if fill["filled_avg_price"] is None and lookup_fn is not None and fill["client_order_id"]:
                try:
                    refreshed = lookup_fn(fill["client_order_id"])
                except Exception:
                    refreshed = None
                if isinstance(refreshed, dict):
                    fill["filled_qty"] = max(
                        fill["filled_qty"], float(refreshed.get("filled_qty") or 0.0))
                    fill["filled_avg_price"] = _opt_float_or_none(refreshed.get("filled_avg_price"))
                    fill["status"] = str(refreshed.get("status") or fill["status"]).lower()
            fills.append(fill)
    try:
        remaining = leg.open_orders()
    except Exception as exc:  # noqa: BLE001 - fail-closed on any fetch failure
        return False, f"open-orders re-fetch failed: {exc}", fills
    still_open = [
        o for o in (remaining or [])
        if str(o.get("symbol")) == symbol
        and str(o.get("order_type") or "").lower() == "trailing_stop"
        and str(o.get("side", "")).lower() == "sell"
        and float(o.get("qty") or 0.0) - float(o.get("filled_qty") or 0.0) > 0
    ]
    if still_open:
        return False, (
            "trailing-stop cancel did not take effect: "
            f"{[o.get('id') for o in still_open]} still open"
        ), fills
    return True, "", fills


def _cancel_trailing_stops_for_exit(broker, symbol, *, trail_stop_id=None, trail_stop_coid=None,
                                    live_trail_stop_id=None, live_trail_stop_coid=None):
    """Cancel the trailing stop for `symbol` on the paper leg AND the
    live leg (a no-op while the live leg is disabled). Both legs must
    confirm; a failure on either vetoes the exit (fail-closed).
    Returns (ok, reason, fills): fills is the confirmed stop fills
    from both legs (B17.3/B17.4)."""
    fills: list[dict] = []
    if broker.paper is not None:
        ok, reason, leg_fills = _cancel_trailing_stop_on_leg(
            broker.paper, symbol, trail_stop_id=trail_stop_id, trail_stop_coid=trail_stop_coid)
        if not ok:
            return False, f"paper: {reason}", fills
        fills.extend(leg_fills)
    if broker.live is not None:
        ok, reason, leg_fills = _cancel_trailing_stop_on_leg(
            broker.live, symbol, trail_stop_id=live_trail_stop_id,
            trail_stop_coid=live_trail_stop_coid)
        if not ok:
            return False, f"live: {reason}", fills
        fills.extend(leg_fills)
    return True, "", fills


def _sync_trailing_stops_after_mirror(broker, guardian, state, adjustments,
                                      cfg=None):
    """Keep trailing stops consistent with the mirror's state changes.

    A trailing stop is a real conditional order sized for the entry's
    full qty. When the mirror removes an entry (0 filled — the order
    expired/cancelled unfilled), its stop would be an orphaned order
    that could short-sell when it fires: cancel it. When the mirror
    reduces an entry to its filled qty, the old stop is sized for the
    full qty: cancel it and re-attach for the filled qty with the
    entry's stored trail percent (falling back to cfg's, when the
    record has none). A failed cancel or re-attach fires a CRITICAL
    alert (unprotected shares) and leaves the entry as-is — the
    end-of-run reconcile remains the backstop. Returns a list of
    {symbol, action, reason} dicts for the run summary.
    """
    synced = []
    if broker is None or broker.paper is None or guardian is None:
        return synced
    for adj in adjustments or []:
        action = adj.get("action")
        if action not in {"removed", "reduced_to_filled"}:
            continue
        symbol = adj.get("symbol")
        record = (getattr(state, "open_entries", None) or {}).get(symbol)
        if record is None:
            continue
        trail_pct = record.get("trail_percent")
        if trail_pct is None and cfg is not None:
            trail_pct = getattr(getattr(cfg, "exits", None), "trail_percent", None)
        trail_stop_id = record.get("trail_stop_id")
        trail_stop_coid = record.get("trail_stop_coid")
        ok, reason, _fills = _cancel_trailing_stop_on_leg(
            broker.paper, symbol, trail_stop_id=trail_stop_id, trail_stop_coid=trail_stop_coid)
        if not ok:
            guardian.alerter.send(
                "critical",
                f"trailing stop for {symbol} could not be cancelled after the "
                f"entry was {action} (filled {adj.get('filled_qty')}): {reason}; "
                "the stop may still fire for shares no longer held")
            synced.append({"symbol": symbol, "action": "stop_cancel_failed",
                           "reason": reason})
            continue
        if action == "removed":
            synced.append({"symbol": symbol, "action": "stop_cancelled",
                           "reason": "entry removed (0 filled)"})
        else:
            # Re-attach for the entry's CURRENT qty — the intent
            # mirror set it to the filled qty, and the B17.2 stop-fill
            # mirror (which runs before this sync) may have reduced
            # it further to the shares actually held. The stop must
            # cover the held shares, not the pre-mirror qty.
            filled_qty = float(record.get("qty") or adj.get("filled_qty") or 0.0)
            if filled_qty <= 0:
                synced.append({"symbol": symbol, "action": "stop_cancelled",
                               "reason": "entry reduced to 0 filled"})
                continue
            reason = ""
            try:
                attach = getattr(broker.paper, "attach_trailing_stop", None)
                stop_coid = f"{record.get('intent_id')}-trail" if record.get("intent_id") else None
                stop = attach(symbol, filled_qty, trail_pct, client_order_id=stop_coid) if attach else None
            except Exception as exc:  # noqa: BLE001 - fail-closed; alert below
                stop = None
                reason = f"re-attach failed: {exc}"
            else:
                reason = "" if stop else "re-attach returned no stop"
            if stop:
                record["trail_stop_id"] = (
                    stop.get("id") if isinstance(stop, dict) else None)
                record["trail_stop_coid"] = (
                    stop.get("client_order_id") if isinstance(stop, dict) else stop_coid)
                record["trail_stop_qty"] = filled_qty
                synced.append({"symbol": symbol, "action": "stop_reattached",
                               "reason": f"filled_qty {filled_qty}"})
            else:
                guardian.alerter.send(
                    "critical",
                    f"trailing stop for {symbol} could not be re-attached "
                    f"for the filled qty {filled_qty} after the entry was "
                    f"reduced to filled: {reason}; the shares are unprotected")
                synced.append({"symbol": symbol, "action": "stop_reattach_failed",
                               "reason": reason})
    return synced


def _opt_float_or_none(value):
    """float(value) or None when unset/unparseable (B17.3/B17.6)."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _stop_fill_state(leg, *, symbol: str, trail_stop_id=None, trail_stop_coid=None,
                     lookup_fn=None) -> dict:
    """A trailing stop's authoritative state BY ORDER ID (Detail 1).

    Returns {"found", "ambiguous", "status", "filled_qty",
    "filled_avg_price", "qty", "client_order_id", "id"}.
    - The per-order lookup (get_by_client_order_id on the stop's
      client_order_id) is authoritative for the stop's final state:
      a stop that filled, was cancelled, or is still working reports
      that status with its cumulative filled_qty.
    - The open-orders list is used ONLY to FIND the stop (its
      client_order_id / qty when the record has none) — never to
      infer its final state from absence: disappearance can mean it
      filled, was canceled, or is temporarily absent from that view.
    - When the stop cannot be confirmed by order id (lookup None /
      raises / unrecognized status) AND it is absent from open
      orders, the state is AMBIGUOUS (found=False, ambiguous=True):
      the caller must keep the exit BLOCKED — no exit submitted for
      the symbol until the stop's final state is confirmed.
    - When no stop is recorded at all (no id/coid) and none is in
      open orders, the stop does not exist: found=False,
      ambiguous=False.
    """
    out = {"found": False, "ambiguous": False, "status": None,
           "filled_qty": 0.0, "filled_avg_price": None, "qty": None,
           "client_order_id": trail_stop_coid, "id": trail_stop_id}
    # 1. The per-order lookup on the known client_order_id.
    if trail_stop_coid and lookup_fn is not None:
        try:
            found = lookup_fn(trail_stop_coid)
        except Exception:
            found = None
        if isinstance(found, dict) and found:
            status = str(found.get("status") or "").lower()
            if status in {"submitted", "new", "partially_filled", "partially filled",
                          "filled", "cancelled", "canceled", "expired", "rejected",
                          "done_for_day"}:
                out.update({
                    "found": True, "status": status,
                    "filled_qty": float(found.get("filled_qty") or 0.0),
                    "filled_avg_price": _opt_float_or_none(found.get("filled_avg_price")),
                    "qty": _opt_float_or_none(found.get("qty")),
                    "id": found.get("id") or trail_stop_id,
                })
                return out
            # An unrecognized status is ambiguous, not a confirmation.
            out["ambiguous"] = True
            return out
        # Inconclusive lookup (None / raises): fall through to the
        # open-orders view. A stop that IS listed there is present,
        # so its state is known even though the lookup failed.
    # 2. The open-orders view: find the stop by id, coid, or symbol.
    try:
        open_orders = leg.open_orders() or []
    except Exception:
        open_orders = []
    stops = [
        o for o in open_orders
        if str(o.get("symbol")) == symbol
        and str(o.get("order_type") or "").lower() == "trailing_stop"
        and str(o.get("side", "")).lower() == "sell"
    ]
    match = None
    if trail_stop_id is not None:
        match = next((o for o in stops if str(o.get("id")) == str(trail_stop_id)), None)
    if match is None and trail_stop_coid is not None:
        match = next((o for o in stops if str(o.get("client_order_id")) == str(trail_stop_coid)), None)
    if match is None and not trail_stop_id and not trail_stop_coid:
        match = stops[0] if stops else None
    if match is not None:
        out.update({
            "found": True,
            "status": str(match.get("status") or "submitted").lower(),
            "filled_qty": float(match.get("filled_qty") or 0.0),
            "filled_avg_price": _opt_float_or_none(match.get("filled_avg_price")),
            "qty": _opt_float_or_none(match.get("qty")),
            "client_order_id": match.get("client_order_id") or trail_stop_coid,
            "id": match.get("id") or trail_stop_id,
        })
        return out
    # Absent from open orders. If a stop was recorded (an id or coid
    # exists), its final state is UNKNOWN — it may have filled, been
    # cancelled, or be temporarily absent from this view. Only when
    # no stop was ever recorded is "no stop" the answer.
    if trail_stop_id is not None or trail_stop_coid is not None:
        out["ambiguous"] = True
    return out


def _mirror_stop_fills_at_startup(broker, guardian, state, cfg=None, memory=None) -> list[dict]:
    """Mirror trailing-stop fills into the strategy state (B17.2).

    At startup (after the intent mirror, before any new submission),
    for each open entry, compare the leg's actual position with the
    entry's expected qty. The stop's filled qty comes from its state
    BY ORDER ID (Detail 1) — never from its absence in open orders.
    - Position LOWER and the stop's filled qty accounts for the
      difference (held == expected - stop_filled, within 1e-6):
      mirror it — reduce the entry to the held qty (or remove it if
      0 held), record the adjustment in stop_sync (with the stop's
      filled qty and price), and do NOT halt.
    - Position lower and the stop's fills do NOT account for the
      difference: genuine drift — do not mirror; the end-of-run
      reconcile halts as today (recorded as unaccounted_shortfall
      for visibility).
    - Position HIGHER than expected: halt as today (do nothing here;
      the reconcile is the backstop).
    - Stop state AMBIGUOUS (lookup inconclusive): do not mirror, and
      mark the entry stop_state_unknown so the exit stays BLOCKED
      across restart — no exit submitted until the stop's final
      state is confirmed.
    B17.3: when the mirror REMOVES an entry (0 held — the stop sold
    everything overnight), the trade is closed, so the decision and
    tax lot are closed here with the stop's fill price and realized
    P&L, in the same fields the exit loop records. (When the entry is
    only REDUCED, the trade stays open and nothing is closed.) If
    the stop's fill price cannot be fetched, realized_pl is recorded
    as UNKNOWN (None) — explicitly not 0 — and a CRITICAL alert
    fires. The exit loop handles the same attribution for stops that
    fill DURING a run; this is the overnight path.
    Returns the stop_sync entries.
    """
    synced: list[dict] = []
    if broker is None or broker.paper is None:
        return synced
    try:
        positions = broker.paper.positions() or {}
    except Exception as exc:  # noqa: BLE001 - fail-closed; the reconcile halts
        log.warning("stop-fill mirror: positions fetch failed: %s", exc)
        return synced
    lookup_fn = getattr(broker.paper, "get_by_client_order_id", None)
    for symbol, record in list((getattr(state, "open_entries", None) or {}).items()):
        expected = float(record.get("qty") or 0.0)
        if expected <= 0:
            continue
        held = abs(float((positions.get(symbol, {}) or {}).get("qty", 0.0) or 0.0))
        stop_state = _stop_fill_state(
            broker.paper, symbol=symbol,
            trail_stop_id=record.get("trail_stop_id"),
            trail_stop_coid=record.get("trail_stop_coid"),
            lookup_fn=lookup_fn,
        )
        if stop_state["ambiguous"]:
            # The stop's final state is unconfirmed: keep the exit
            # BLOCKED across restart (Detail 1). The flag is cleared
            # by the next startup that confirms the stop by order id.
            record["stop_state_unknown"] = True
            synced.append({
                "symbol": symbol, "action": "stop_state_unknown",
                "reason": "stop's final state unconfirmed by order-id lookup; exit blocked",
            })
            continue
        record.pop("stop_state_unknown", None)
        if not stop_state["found"]:
            # No stop exists for this entry: nothing to mirror (the
            # position difference, if any, is genuine drift).
            continue
        stop_filled = float(stop_state["filled_qty"] or 0.0)
        # (c) trace: a previous run requested this stop's cancellation
        # but its response was dropped, so the cancel's status was
        # UNKNOWN and the exit stayed blocked. The stop is now
        # confirmed terminal BY ORDER ID — record the confirmation and
        # the unprotected window it bounds. The window is UNCERTAIN:
        # the exact moment the stop stopped protecting is unknowable
        # while its status was unknown, so the true unprotected
        # duration is bounded by the cancel-request time and this
        # confirmation time.
        if (stop_state["status"] in ("cancelled", "filled", "expired", "rejected")
                and record.get("stop_cancel_requested_at")):
            confirmed_at = datetime.now(timezone.utc).isoformat()
            synced.append({
                "symbol": symbol, "action": "stop_cancel_confirmed",
                "stop_client_order_id": stop_state["client_order_id"],
                "stop_status": stop_state["status"],
                "stop_filled_qty": stop_filled,
                "unprotected_window": {
                    "start": record["stop_cancel_requested_at"],
                    "end": confirmed_at,
                    "uncertain": True,
                    "note": ("the stop's cancellation was requested in the previous run but "
                             "its response was dropped; the exact moment the stop stopped "
                             "protecting is unknowable while its status was unknown — the "
                             "true unprotected duration is bounded by the cancel-request "
                             "time and the confirmation time"),
                },
                "reason": "stop confirmed terminal by order-id lookup after an unconfirmed cancel",
            })
            record.pop("stop_cancel_requested_at", None)
        shortfall = expected - held
        if shortfall <= 1e-6:
            continue  # position matches the entry; nothing to mirror
        if held > expected:
            continue  # higher than expected: the reconcile halts as today
        if stop_filled <= 0:
            synced.append({
                "symbol": symbol, "action": "unaccounted_shortfall",
                "reason": f"position {held} < expected {expected} but the stop filled 0; genuine drift",
                "expected_qty": expected, "held_qty": held, "stop_filled_qty": 0.0,
            })
            continue
        if abs(shortfall - stop_filled) > 1e-6:
            synced.append({
                "symbol": symbol, "action": "unaccounted_shortfall",
                "reason": (
                    f"position {held} < expected {expected} (shortfall {shortfall}) "
                    f"but the stop filled only {stop_filled}; genuine drift"),
                "expected_qty": expected, "held_qty": held, "stop_filled_qty": stop_filled,
            })
            continue
        # The stop's fills account for the whole shortfall: mirror it.
        stop_price = stop_state["filled_avg_price"]
        decision_closed = False
        realized_pl = None
        if held <= 1e-6:
            state.open_entries.pop(symbol, None)
            state.expected_positions.get("paper", {}).pop(symbol, None)
            state.expected_positions.get("live", {}).pop(symbol, None)
            action = "stop_fill_mirrored_removed"
            # B17.3 (overnight path): the trade is closed — close the
            # decision and tax lot with the stop's fill price and P&L,
            # in the same fields the exit loop records.
            if memory is not None and record.get("decision_id") is not None:
                entry_price = float(record.get("entry_price") or 0.0)
                try:
                    opened_at = datetime.fromisoformat(record["opened_at"])
                    holding_days = (
                        datetime.now(timezone.utc) - opened_at
                    ).total_seconds() / 86400.0
                except Exception:  # noqa: BLE001 - opened_at unparseable
                    holding_days = 0.0
                if stop_price is not None:
                    realized_pl = (stop_price - entry_price) * stop_filled
                    proceeds = stop_price * stop_filled
                else:
                    realized_pl = None
                    proceeds = None
                    guardian.alerter.send(
                        "critical",
                        f"startup stop-fill mirror for {symbol}: the trailing stop sold "
                        f"{stop_filled} shares but its fill price could not be fetched; "
                        "realized_pl is recorded as UNKNOWN (not 0)",
                    )
                memory.close_decision(
                    record["decision_id"], stop_price, realized_pl, holding_days)
                if record.get("lot_id"):
                    memory.close_tax_lot(record["lot_id"], proceeds)
                decision_closed = True
        else:
            record["qty"] = held
            expected_pos = state.expected_positions.get("paper", {})
            if symbol in expected_pos:
                expected_pos[symbol]["qty"] = held
            action = "stop_fill_mirrored_reduced"
        synced.append({
            "symbol": symbol, "action": action,
            "expected_qty": expected, "held_qty": held,
            "stop_filled_qty": stop_filled,
            "stop_filled_avg_price": stop_price,
            "stop_client_order_id": stop_state["client_order_id"],
            "exposure_ended": "sold",
            "exposure_shares": stop_filled,
            "decision_closed": decision_closed,
            "realized_pl": None if realized_pl is None else round(realized_pl, 2),
            "reason": "stop's fills account for the shortfall; entry mirrored, no halt",
        })
    return synced


def _sync_trailing_stop_sizes_at_startup(broker, guardian, state, cfg=None) -> list[dict]:
    """Size every trailing stop to the FILLED qty (Detail 3).

    The protective quantity is the shares actually held — not the
    entry qty. At attach time (the entry loop) the stop is sized to
    the buy's filled qty; as later fills arrive, the stop is resized
    (cancel + re-attach) to the new filled qty. This startup sync
    enforces it for every open entry: the stop's remaining qty
    (qty - filled_qty, from its state BY ORDER ID) must equal the
    shares held. A stop larger than held is oversized — it could
    short-sell when it triggers; a stop smaller than held leaves
    shares unprotected.

    RESIZE ORDER: cancel-old-then-attach-new. The old stop's
    cancellation is confirmed BY ORDER ID before the new stop is
    attached, so two independently executable stops never protect
    the same shares twice (attach-new-then-cancel-old would create
    a double-protection window where both stops could fire and
    oversell). The cost is a brief UNPROTECTED window between the
    old stop's cancel confirmation and the new stop's attach — it is
    recorded explicitly in the resize trace (start/end). If the old
    cancel cannot be confirmed by order id, the resize is DEFERRED
    (no new stop is attached; the old stop stays working) — the
    entry keeps its old (possibly undersized) stop rather than
    risking two live stops.

    The resize trace reports, at EACH step, BOTH numbers side by
    side: (a) the total quantity the working stops could sell if
    they all triggered (the sum of qty of every stop still working
    / not yet confirmed cancelled), and (b) the shares actually
    held. With cancel-old-then-attach-new, (a) never exceeds (b).
    """
    synced: list[dict] = []
    if broker is None or broker.paper is None or guardian is None:
        return synced
    lookup_fn = getattr(broker.paper, "get_by_client_order_id", None)
    attach = getattr(broker.paper, "attach_trailing_stop", None)
    # The protective quantity is the shares actually HELD (the
    # broker-confirmed position), NOT the entry record's qty: a
    # partially-filled buy's record still carries the entry qty (the
    # intent mirror only mirrors cancelled intents, and the B17.2
    # mirror only acts when a stop exists), so sizing the stop to the
    # record would attach an OVERSIZED stop — the exact Detail 3
    # failure (a 2-share stop on 1 held share, which shorts on
    # trigger with the cap off). If positions() cannot be fetched,
    # resize nothing: do not attach a stop sized to a guessed qty.
    try:
        positions = broker.paper.positions() or {}
    except Exception as exc:  # noqa: BLE001 - fail-closed; the reconcile halts
        log.warning("stop-size sync: positions fetch failed: %s", exc)
        return synced
    for symbol, record in list((getattr(state, "open_entries", None) or {}).items()):
        held = abs(float((positions.get(symbol, {}) or {}).get("qty", 0.0) or 0.0))
        trail_pct = record.get("trail_percent")
        if trail_pct is None and cfg is not None:
            trail_pct = getattr(getattr(cfg, "exits", None), "trail_percent", None)
        stop_state = _stop_fill_state(
            broker.paper, symbol=symbol,
            trail_stop_id=record.get("trail_stop_id"),
            trail_stop_coid=record.get("trail_stop_coid"),
            lookup_fn=lookup_fn,
        )
        if stop_state["ambiguous"]:
            record["stop_state_unknown"] = True
            synced.append({
                "symbol": symbol, "action": "stop_size_unknown",
                "reason": "stop's final state unconfirmed by order-id lookup; resize deferred, exit blocked",
                "held_qty": held,
            })
            continue
        record.pop("stop_state_unknown", None)
        # The stop's remaining (working) qty: its nominal qty minus
        # what it already sold. The nominal qty comes from the state
        # (open orders when working) or the entry's recorded attach
        # qty; a terminal stop protects nothing.
        status = (stop_state["status"] or "").lower()
        if not stop_state["found"] or status in {"filled", "cancelled", "canceled",
                                                 "expired", "rejected"}:
            covered = 0.0
            stop_qty = 0.0
        else:
            stop_qty = stop_state["qty"]
            if stop_qty is None:
                stop_qty = float(record.get("trail_stop_qty") or 0.0)
            covered = max(0.0, float(stop_qty or 0.0) - float(stop_state["filled_qty"] or 0.0))
        if abs(covered - held) <= 1e-6:
            # The stop already covers exactly the shares held.
            record["trail_stop_qty"] = stop_qty if stop_state["found"] else record.get("trail_stop_qty")
            continue
        now = datetime.now(timezone.utc).isoformat()
        if held <= 1e-6:
            # Nothing held: an orphaned stop could short-sell. Cancel it.
            if stop_state["found"]:
                ok, reason, _fills = _cancel_trailing_stop_on_leg(
                    broker.paper, symbol,
                    trail_stop_id=record.get("trail_stop_id"),
                    trail_stop_coid=record.get("trail_stop_coid"),
                    lookup_fn=lookup_fn)
                if ok:
                    record["trail_stop_id"] = None
                    record["trail_stop_coid"] = None
                    record["trail_stop_qty"] = None
                    synced.append({"symbol": symbol, "action": "stop_cancelled",
                                   "reason": "nothing held; orphaned stop cancelled",
                                   "held_qty": 0.0, "stop_qty": stop_qty})
                else:
                    guardian.alerter.send(
                        "critical",
                        f"orphaned trailing stop for {symbol} could not be cancelled: {reason}")
                    synced.append({"symbol": symbol, "action": "stop_cancel_failed",
                                   "reason": reason, "held_qty": 0.0, "stop_qty": stop_qty})
            continue
        # A resize (or a first attach) is needed. Trace each step.
        trace: list[dict] = []
        old_qty = float(stop_qty or 0.0) if stop_state["found"] else 0.0
        if stop_state["found"] and status not in {"filled", "cancelled", "canceled",
                                                   "expired", "rejected"}:
            # Step 1: the old stop is still working (its cancel has
            # not been requested yet). Sellable = the old stop's
            # remaining qty; held may exceed it (a later fill arrived).
            trace.append({"step": 1, "action": "resize_start_old_stop_working",
                          "sellable_qty": round(old_qty - float(stop_state["filled_qty"] or 0.0), 4),
                          "held_qty": held,
                          "unprotected_shares": round(max(0.0, held - (old_qty - float(stop_state["filled_qty"] or 0.0))), 4),
                          "ts": now})
            # Cancel the old stop, and confirm BY ORDER ID (Detail 1)
            # before attaching the new one — never by its absence
            # from the open-orders list.
            ok, reason, _fills = _cancel_trailing_stop_on_leg(
                broker.paper, symbol,
                trail_stop_id=record.get("trail_stop_id"),
                trail_stop_coid=record.get("trail_stop_coid"),
                lookup_fn=lookup_fn)
            cancel_confirmed_at = datetime.now(timezone.utc).isoformat()
            if not ok:
                # The old cancel is unconfirmed: DEFER the resize.
                # The old stop stays working (possibly undersized);
                # no new stop is attached, so two live stops can
                # never protect the same shares twice.
                synced.append({
                    "symbol": symbol, "action": "stop_resize_deferred",
                    "reason": f"old stop cancel unconfirmed: {reason}",
                    "held_qty": held, "old_stop_qty": old_qty,
                    "resize_trace": trace,
                })
                continue
            # Step 2: the old stop's cancel is confirmed by order id.
            # No stop is working: sellable 0, all held shares are
            # unprotected until the new stop attaches.
            trace.append({"step": 2, "action": "old_stop_cancel_confirmed",
                          "sellable_qty": 0.0, "held_qty": held,
                          "unprotected_shares": held,
                          "ts": cancel_confirmed_at})
        else:
            # No working stop (a first attach, or the old stop already
            # terminated): the shares have been unprotected since the
            # fill arrived — bounded by the last run (uncertain: the
            # exact fill time is unknowable from the store).
            window_start = getattr(state, "last_run_ts", None)
            trace.append({"step": 1, "action": "attach_start_no_working_stop",
                          "sellable_qty": 0.0, "held_qty": held,
                          "unprotected_shares": held,
                          "ts": now,
                          "unprotected_since": window_start,
                          "unprotected_since_uncertain": True})
            cancel_confirmed_at = None
        # Attach the new stop sized to the FILLED qty (held).
        stop_coid = None
        intent_id = record.get("intent_id")
        if intent_id:
            stop_coid = f"{intent_id}-trail"
        new_stop = None
        attach_reason = ""
        try:
            new_stop = attach(symbol, held, trail_pct, client_order_id=stop_coid) if attach else None
        except Exception as exc:  # noqa: BLE001 - fail-closed; alert below
            new_stop = None
            attach_reason = f"attach failed: {exc}"
        else:
            attach_reason = "" if new_stop else "attach returned no stop"
        attached_at = datetime.now(timezone.utc).isoformat()
        if not new_stop:
            guardian.alerter.send(
                "critical",
                f"trailing stop for {symbol} could not be attached for the filled qty "
                f"{held}: {attach_reason}; the shares are unprotected")
            synced.append({"symbol": symbol, "action": "stop_attach_failed",
                           "reason": attach_reason, "held_qty": held,
                           "resize_trace": trace})
            continue
        record["trail_stop_id"] = new_stop.get("id") if isinstance(new_stop, dict) else None
        record["trail_stop_coid"] = (new_stop.get("client_order_id") if isinstance(new_stop, dict)
                                     else stop_coid)
        record["trail_stop_qty"] = held
        # Step 3 (or 2): the new stop is working, sized to held.
        trace.append({"step": len(trace) + 1, "action": "new_stop_attached",
                      "sellable_qty": held, "held_qty": held,
                      "unprotected_shares": 0.0, "ts": attached_at})
        window = None
        if cancel_confirmed_at is not None:
            window = {"start": cancel_confirmed_at, "end": attached_at,
                      "seconds": round(
                          (datetime.fromisoformat(attached_at)
                           - datetime.fromisoformat(cancel_confirmed_at)).total_seconds(), 3),
                      "uncertain": False,
                      "note": "cancel-old-then-attach-new: the shares were unprotected "
                              "between the old stop's cancel confirmation and the new stop's attach"}
        else:
            window = {"start": getattr(state, "last_run_ts", None), "end": attached_at,
                      "seconds": None, "uncertain": True,
                      "note": "no working stop: the shares were unprotected since the fill arrived "
                              "(bounded by the last run; the exact fill time is unknowable from the store)"}
        synced.append({
            "symbol": symbol,
            "action": "stop_attached" if old_qty <= 0 else "stop_resized",
            "old_stop_qty": old_qty, "new_stop_qty": held, "held_qty": held,
            "resize_trace": trace, "unprotected_window": window,
            "reason": "stop sized to the filled qty (the protective quantity)",
        })
    return synced


def _protection_record(broker, manager, state, symbol: str, pending: dict) -> dict:
    """Held / exit-pending / protected for one pending exit, from the
    broker's CURRENT state (never from memory): held = the leg's
    position; exit_pending = the exit order's remaining qty (qty -
    filled_qty from the intent store); protected = the working stop's
    remaining qty (0 when no stop is working). Unprotected = held -
    protected. The exposure start is the PERSISTED
    exposure_started_at (the same value across restarts — the record
    is durable in the state file, Detail 2)."""
    account = str(pending.get("account") or "paper")
    leg = getattr(broker, account, None)
    held = 0.0
    if leg is not None:
        try:
            held = abs(float(((leg.positions() or {}).get(symbol, {}) or {}).get("qty", 0.0) or 0.0))
        except Exception:
            held = 0.0
    exit_pending = float(pending.get("qty") or 0.0) - float(pending.get("filled_qty") or 0.0)
    protected = 0.0
    record = (getattr(state, "open_entries", None) or {}).get(symbol)
    if record is not None and leg is not None:
        stop_state = _stop_fill_state(
            leg, symbol=symbol,
            trail_stop_id=record.get("trail_stop_id"),
            trail_stop_coid=record.get("trail_stop_coid"),
            lookup_fn=getattr(leg, "get_by_client_order_id", None))
        if stop_state["found"] and str(stop_state["status"] or "").lower() in {
                "submitted", "new", "partially_filled", "partially filled"}:
            stop_qty = stop_state["qty"]
            if stop_qty is None:
                stop_qty = float(record.get("trail_stop_qty") or 0.0)
            protected = max(0.0, float(stop_qty or 0.0) - float(stop_state["filled_qty"] or 0.0))
    unprotected = max(0.0, held - protected)
    return {
        "symbol": symbol, "account": account,
        "held": round(held, 4),
        "exit_pending": round(max(0.0, exit_pending), 4),
        "protected": round(protected, 4),
        "unprotected": round(unprotected, 4),
        "exit_intent_id": pending.get("exit_intent_id"),
        "exposure_started_at": pending.get("exposure_started_at"),
        "exposure_uncertain": bool(pending.get("exposure_uncertain")),
        "exposure_ended_at": pending.get("exposure_ended_at"),
        "exposure_end_reason": pending.get("exposure_end_reason"),
    }


def _sync_pending_exits(broker, manager, state, guardian) -> list[dict]:
    """Reconcile the durable pending-exit records (B17.2 / Detail 2).

    For each pending exit, look its exit intent up in the store:
    - FILLED: the exposure ends ("sold"); the record is removed.
    - CANCELLED / REJECTED: the exit order is gone but the shares
      may still be held — alert, re-create the entry (so the exit
      can be re-derived next run), and remove the record. The
      re-created entry carries exit_attempts+1, so the re-derived
      exit gets a fresh intent id (the rejected intent's id is
      terminal and can never be re-submitted).
    - UNKNOWN: keep the record — the next run retries the LOOKUP
      only; the exit itself is never re-submitted (exact-once).
    - SUBMITTED / PARTIALLY_FILLED: keep the record, update the
      filled qty from the intent, and report the protection numbers
      (held / exit-pending / protected / unprotected). A partial
      fill reduces the unprotected count by EXACTLY the sold qty.
    Returns the protection records for the run summary.
    """
    protection: list[dict] = []
    if manager is None:
        return protection
    for key, pending in list((getattr(state, "pending_exits", None) or {}).items()):
        symbol = str(pending.get("symbol") or key)
        intent_id = pending.get("exit_intent_id")
        intent = manager.get_intent(intent_id) if intent_id else None
        if intent is None:
            guardian.alerter.send(
                "critical",
                f"pending exit for {symbol} has no intent {intent_id} in the store; "
                "the exposure record is dropped — the symbol needs a human look")
            state.pending_exits.pop(key, None)
            protection.append({**_protection_record(broker, manager, state, symbol, pending),
                               "action": "pending_exit_intent_missing"})
            continue
        status = str(intent.get("status") or "")
        if status == "FILLED":
            pending["exposure_ended_at"] = datetime.now(timezone.utc).isoformat()
            pending["exposure_end_reason"] = "sold"
            state.pending_exits.pop(key, None)
            # The leg is flat now: clear its expected position (it
            # was kept when the live exit was UNKNOWN — see the exit
            # loop; for paper it was already popped there, so this
            # is a no-op).
            _account = str(pending.get("account") or "paper")
            state.expected_positions.get(_account, {}).pop(symbol, None)
            protection.append({**_protection_record(broker, manager, state, symbol, pending),
                               "action": "pending_exit_filled"})
            continue
        if status in {"CANCELLED", "REJECTED"}:
            # The exit order is gone but the shares may still be held:
            # re-create the entry so the exit can be re-derived next
            # run (with a fresh intent id — see exit_attempts).
            account = str(pending.get("account") or "paper")
            leg = getattr(broker, account, None)
            held = 0.0
            if leg is not None:
                try:
                    held = abs(float(((leg.positions() or {}).get(symbol, {}) or {}).get("qty", 0.0) or 0.0))
                except Exception:
                    held = 0.0
            if held > 0:
                state.open_entries[symbol] = {
                    "symbol": symbol, "qty": held, "side": "buy",
                    "decision_id": pending.get("decision_id"),
                    "lot_id": pending.get("lot_id"),
                    "entry_price": pending.get("entry_price"),
                    "opened_at": pending.get("opened_at"),
                    "intent_id": None, "trail_stop_id": None,
                    "trail_stop_coid": None, "trail_stop_qty": None,
                    "trail_percent": pending.get("trail_percent"),
                    "exit_attempts": int(pending.get("exit_attempts") or 0) + 1,
                }
                expected = state.expected_positions.setdefault(account, {})
                expected[symbol] = {"qty": held, "decision_id": pending.get("decision_id")}
            guardian.alerter.send(
                "critical",
                f"exit for {symbol} ({account}) ended {status} while {held} shares are "
                "still held; the entry was re-created so the exit can be re-derived")
            state.pending_exits.pop(key, None)
            protection.append({**_protection_record(broker, manager, state, symbol, pending),
                               "action": "pending_exit_ended_entry_recreated",
                               "exit_status": status})
            continue
        if status == "UNKNOWN":
            # The exit's outcome is still unknown: keep the record.
            # The next run retries the LOOKUP only — the exit itself
            # is never re-submitted.
            protection.append({**_protection_record(broker, manager, state, symbol, pending),
                               "action": "pending_exit_unknown"})
            continue
        # SUBMITTED / PARTIALLY_FILLED (or any other working state):
        # update the filled qty from the intent and report the numbers.
        pending["filled_qty"] = float(intent.get("filled_qty") or 0.0)
        protection.append({**_protection_record(broker, manager, state, symbol, pending),
                           "action": "pending_exit_working"})
    return protection


def _check_exit_oversell(broker, symbol: str, stop_fills: list, exit_qty: float,
                         held_at_exit, guardian, manager=None) -> dict | None:
    """B17.4: after the exit submits, re-check the stop BY ORDER ID.

    Returns the detection record {symbol, stop_filled_qty, exit_qty,
    held_at_exit, total_committed, note} when an oversell was detected
    (and halts + alerts), else None.

    DETECTION, NOT PREVENTION — the oversell has already happened by
    the time this runs: the stop elected between the cancel
    confirmation and the exit submit, and both orders are now working
    / filled. This check detects it and halts the guardian so no
    further trading compounds it. Prevention (OCO/bracket orders)
    is OUT OF SCOPE for B17 (deferred by the user).
    The check: for each stop cancelled before the exit, compare its
    filled qty at cancel time with a FRESH lookup after the submit.
    If the stop filled more in that window (delta) and the total
    committed to sell (the stop's new fills + this exit's qty)
    exceeds the position held at exit time, the total sold WILL
    exceed the position — halt + CRITICAL alert. If the post-submit
    lookup is inconclusive (None / raises), fail closed the same way:
    the stop's state after the submit is unknown, and it may have
    sold shares in the window. The end-of-run reconcile is the
    backstop (a negative position trips the drift halt).
    """
    if held_at_exit is None or float(exit_qty or 0.0) <= 0:
        return None
    lookup_fn = getattr(broker.paper, "get_by_client_order_id", None)
    if lookup_fn is None:
        return None
    held = float(held_at_exit)
    for fill in stop_fills or []:
        coid = fill.get("client_order_id")
        if not coid:
            continue
        before = float(fill.get("filled_qty") or 0.0)
        try:
            now_state = lookup_fn(coid)
        except Exception as exc:
            reason = (f"OVERSELL CHECK for {symbol}: the trailing stop {coid} "
                      f"could not be looked up after the exit submitted ({exc}); "
                      "its state is unknown — it may have sold shares in the window. "
                      "DETECTION, not prevention: halting the guardian.")
            guardian.manual_halt(reason)
            guardian.alerter.send("critical", reason)
            return {
                "symbol": symbol, "stop_filled_qty": None,
                "exit_qty": float(exit_qty), "held_at_exit": held,
                "total_committed": None, "note": reason,
            }
        if not isinstance(now_state, dict):
            reason = (f"OVERSELL CHECK for {symbol}: the trailing stop {coid} "
                      "could not be found after the exit submitted (inconclusive lookup); "
                      "its state is unknown — it may have sold shares in the window. "
                      "DETECTION, not prevention: halting the guardian.")
            guardian.manual_halt(reason)
            guardian.alerter.send("critical", reason)
            return {
                "symbol": symbol, "stop_filled_qty": None,
                "exit_qty": float(exit_qty), "held_at_exit": held,
                "total_committed": None, "note": reason,
            }
        after = float(now_state.get("filled_qty") or 0.0)
        delta = after - before
        if delta <= 1e-6:
            continue
        total_committed = delta + float(exit_qty)
        if total_committed > held + 1e-6:
            reason = (
                f"OVERSELL DETECTED for {symbol}: the trailing stop sold {delta} shares "
                f"between the cancel confirmation and the exit submit, and the exit "
                f"orders {exit_qty} more, against {held} held (total committed "
                f"{total_committed} > {held}). DETECTION, not prevention — the "
                "oversell has already happened; halting the guardian so no further "
                "trading compounds it. Prevention (OCO/bracket orders) is deferred.")
            guardian.manual_halt(reason)
            guardian.alerter.send("critical", reason)
            return {
                "symbol": symbol, "stop_filled_qty": delta,
                "exit_qty": float(exit_qty), "held_at_exit": held,
                "total_committed": total_committed, "note": reason,
            }
    return None


def _protection_records_now(broker, manager, state) -> dict:
    """Protection records for every pending exit, from the broker's
    CURRENT state (read-only — no sync, no mutation). Keyed by
    f"{account}:{symbol}". Used at the end of a run so the summary
    reports the exits created during the run too, not just the ones
    pending at startup."""
    out = {}
    if manager is None:
        return out
    for key, pending in list((getattr(state, "pending_exits", None) or {}).items()):
        symbol = str(pending.get("symbol") or key)
        intent = manager.get_intent(pending.get("exit_intent_id")) if pending.get("exit_intent_id") else None
        action = "pending_exit_working"
        if intent is not None and str(intent.get("status") or "") == "UNKNOWN":
            action = "pending_exit_unknown"
            pending["filled_qty"] = float(intent.get("filled_qty") or 0.0)
        elif intent is not None:
            pending["filled_qty"] = float(intent.get("filled_qty") or 0.0)
        record = _protection_record(broker, manager, state, symbol, pending)
        record["action"] = action
        out[key] = record
    return out


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
    stop_sync: list[dict] = []
    protection: list[dict] = []
    stop_attribution: list[dict] = []
    oversell_detections: list[dict] = []
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
            # DONE_FOR_DAY intents are included: a fill overnight
            # adopts to FILLED, a confirmed expired/canceled adopts
            # to CANCELLED, and a still-done_for_day intent stays
            # DONE_FOR_DAY (its reservation is preserved; no
            # replacement is submitted while it is unresolved).
            manager.reconcile_open_intents(lookup_fn=lookup_fn)
            # B17.5: the live leg's startup reconcile (account="live"),
            # only when the live leg is enabled. A disabled live leg
            # has no intents — nothing to reconcile, and no live
            # intent is ever created for it.
            if _live_leg_enabled(broker):
                live_lookup_fn = getattr(broker.live, "get_by_client_order_id", None)
                if live_lookup_fn is not None:
                    manager.reconcile_all_unknowns(
                        lookup_fn=live_lookup_fn, account="live",
                        grace_seconds=0.0, max_polls=3, sleep_fn=lambda s: None,
                    )
                    manager.reconcile_open_intents(lookup_fn=live_lookup_fn, account="live")
            # Dry-run is a preview and must not mutate strategy state.
            if not dry_run:
                state_sync = _mirror_cancelled_intents_into_state(
                    state, manager.cancelled_intents()
                )
                for adj in state_sync:
                    log.info("startup state sync: %s", adj)
                # B17.2: mirror trailing-stop fills into the state
                # (a stop firing with no signal-loss exit leaves a
                # stale entry; the stop's fills account for the
                # shortfall, so mirror it instead of halting).
                # Runs BEFORE the stop sync below, so the re-attach
                # sizes the stop to the shares actually held.
                _b172 = _mirror_stop_fills_at_startup(
                    broker, guardian, state, cfg, memory=memory)
                stop_sync.extend(_b172)
                for entry in _b172:
                    log.info("startup trailing-stop sync: %s", entry)
                # A trailing stop is a real order sized for the
                # entry's full qty: cancel/re-attach it to match the
                # mirrored entry (an orphaned stop could short-sell).
                _b16sync = _sync_trailing_stops_after_mirror(
                    broker, guardian, state, state_sync, cfg=cfg)
                stop_sync.extend(_b16sync)
                for entry in _b16sync:
                    log.info("startup trailing-stop sync: %s", entry)
                # Detail 3: size every trailing stop to the FILLED
                # qty (the protective quantity) — attach one when
                # the first fill arrives, resize as later fills
                # arrive (cancel-old-then-attach-new).
                _sizes = _sync_trailing_stop_sizes_at_startup(
                    broker, guardian, state, cfg)
                stop_sync.extend(_sizes)
                for entry in _sizes:
                    log.info("startup trailing-stop sync: %s", entry)
                # B17.2 / Detail 2: reconcile the durable pending-exit
                # records (accepted-but-unfilled exits, and UNKNOWN
                # exits awaiting a lookup-only retry).
                protection = _sync_pending_exits(broker, manager, state, guardian)
                for entry in protection:
                    log.info("startup pending-exit sync: %s", entry)
    # B16 correction: intents still DONE_FOR_DAY after the startup
    # reconcile — unresolved for the day, their unfilled qty stays
    # reserved and no replacement may be submitted for their symbol.
    # Listed in the summary so the run shows what it is waiting on.
    done_for_day: list[dict] = []
    if manager is not None:
        done_for_day = [
            {
                "symbol": row.get("symbol"),
                "intent_id": row.get("intent_id"),
                "filled_qty": row.get("filled_qty"),
                "remaining_qty": round(
                    float(row.get("qty") or 0.0) - float(row.get("filled_qty") or 0.0), 4),
            }
            for row in manager.done_for_day_intents()
        ]

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
    # B16 correction: a DONE_FOR_DAY buy is excluded from the
    # broker's OPEN query, so its unfilled qty would otherwise not be
    # reserved at all — the intent store is the source for that qty.
    # The merge dedupes by broker order id, so an order the broker
    # still lists as open is counted once (via the broker list, as
    # today); a DONE_FOR_DAY intent's remaining qty is counted from
    # the intent store. Priced with the same chain as any open buy.
    if manager is not None and broker.paper is not None:
        _paper_lookup = getattr(broker.paper, "get_by_client_order_id", None)
        _sizing_orders = _open_orders_including_done_for_day(
            broker.paper, manager, _paper_lookup, account="paper")
    else:
        _sizing_orders = _open_orders_for_leg(broker.paper)
    reservation_total, reservation_detail = _open_buy_reservations(
        _sizing_orders, strategy_symbols,
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
            # The BLOCKED intent this creates was never submitted; the
            # strategy re-derives the same DECISION next run, and once
            # the blocking intent resolves, that run submits it for
            # the first time. (OrderManager.submit would also refuse
            # it as BLOCKED; pre-checking here keeps the refusal out
            # of the guardian accounting and the decision record
            # entirely.)
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
        live_result = None
        live_intent = None
        if not broker.live.floor_tripped():
            if manager is not None and _live_leg_enabled(broker):
                # B17.5: the live leg is enabled — route the live
                # entry through the OrderManager (account="live",
                # with the live leg's lookup), the same exact-once
                # protection the paper leg has. The live intent is a
                # separate broker order from the paper one (the
                # account is part of the idempotency key).
                live_intent = _live_intent(
                    order,
                    {
                        "symbol": symbol,
                        "direction": direction,
                        "decision_score": round(float(item["decision_score"]), 6),
                        "score": round(float(item["score"]), 6),
                        "trend_quality": round(float(item["trend_quality"]), 6),
                        "last_price": round(float(price_for_sizing), 2),
                        "target_notional": round(float(capped_notional), 2),
                        "intended_notional": round(float(intended_notional), 2),
                    },
                    today,
                    account="live",
                )
                live_result = guardian.submit(
                    order,
                    lambda o, _intent=live_intent: _submit_live_via_manager(o, _intent, manager, broker),
                )
            else:
                # Disabled live leg: the direct no-op call (records
                # the invocation, places nothing, creates no intent).
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
        # B16 reopen: record the trailing stop's broker id and percent
        # in the entry, so the startup sync can cancel/re-attach it
        # when the mirror changes the entry, and the exit can cancel
        # it by id.
        # B17 / Detail 3: size the stop to the FILLED qty (the
        # protective quantity), not the entry qty. A limit buy that
        # has not filled yet holds nothing, so no stop is attached
        # for it — the startup size sync attaches one when the first
        # fill arrives. An oversized stop (attached for the entry qty
        # while only part filled) could short-sell when it triggers.
        # The filled qty is the broker-confirmed filled qty from the
        # intent store (0 for an unfilled limit buy at submit time).
        # The stop's client_order_id is deterministic
        # ("<entry intent id>-trail") so its final state can be
        # confirmed BY ORDER ID (Detail 1).
        trail_stop_id = None
        trail_stop_coid = None
        trail_stop_qty = None
        live_trail_stop_id = None
        live_trail_stop_coid = None
        if side == "buy" and paper_result is not None and not dry_run:
            filled_qty = 0.0
            if manager is not None and intent is not None:
                stored = manager.get_intent(intent["intent_id"])
                if stored is not None:
                    filled_qty = float(stored.get("filled_qty") or 0.0)
            if filled_qty > 0:
                stop_coid = f"{intent['intent_id']}-trail" if intent is not None else None
                paper_stop = broker.paper.attach_trailing_stop(
                    symbol, filled_qty, trail_percent, client_order_id=stop_coid)
                if isinstance(paper_stop, dict):
                    trail_stop_id = paper_stop.get("id")
                    trail_stop_coid = paper_stop.get("client_order_id") or stop_coid
                    trail_stop_qty = filled_qty
        if side == "buy" and live_result is not None and not dry_run:
            live_filled_qty = 0.0
            if manager is not None and live_intent is not None:
                stored = manager.get_intent(live_intent["intent_id"])
                if stored is not None:
                    live_filled_qty = float(stored.get("filled_qty") or 0.0)
            if live_filled_qty > 0:
                live_stop_coid = f"{live_intent['intent_id']}-trail" if live_intent is not None else None
                live_stop = broker.live.attach_trailing_stop(
                    symbol, live_filled_qty, trail_percent, client_order_id=live_stop_coid)
                if isinstance(live_stop, dict):
                    live_trail_stop_id = live_stop.get("id")
                    live_trail_stop_coid = live_stop.get("client_order_id") or live_stop_coid
        if side == "buy":
            state.open_entries[symbol]["trail_percent"] = trail_percent
            state.open_entries[symbol]["trail_stop_id"] = trail_stop_id
            state.open_entries[symbol]["trail_stop_coid"] = trail_stop_coid
            state.open_entries[symbol]["trail_stop_qty"] = trail_stop_qty
            state.open_entries[symbol]["live_trail_stop_id"] = live_trail_stop_id
            state.open_entries[symbol]["live_trail_stop_coid"] = live_trail_stop_coid

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
        # Detail 1: the stop's final state is unconfirmed (the startup
        # mirror marked it) — the exit stays BLOCKED across restart.
        # No exit is submitted for the symbol until the stop's final
        # state is confirmed by order id.
        if record.get("stop_state_unknown"):
            skipped.append({
                "symbol": symbol,
                "stage": "exit_stop_unknown",
                "reason": "trailing-stop final state unconfirmed by order-id lookup; exit blocked",
            })
            log.info("skipping exit for %s: trailing-stop state unknown", symbol)
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
        stop_fills: list[dict] = []
        stop_cancelled_at = None
        held_at_exit = None
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
            # B16 reopen: cancel the trailing stop BEFORE submitting
            # the exit. The stop and the exit can both sell the same
            # held share — if the stop is still open when the exit
            # submits, it may fire later and short-sell shares the
            # entry no longer holds. If the stop cannot be confirmed
            # cancelled, the exit is skipped and the entry stays open,
            # so the stop remains in place protecting the shares.
            # The cancel returns the stop's fills (B17.3/B17.4): what
            # it sold, and at what price.
            stop_cancel_attempted_at = datetime.now(timezone.utc).isoformat()
            ok, stop_reason, stop_fills = _cancel_trailing_stops_for_exit(
                broker, symbol,
                trail_stop_id=record.get("trail_stop_id"),
                trail_stop_coid=record.get("trail_stop_coid"),
                live_trail_stop_id=record.get("live_trail_stop_id"),
                live_trail_stop_coid=record.get("live_trail_stop_coid"))
            if not ok:
                # (c) trace: the cancel was requested but its status
                # is UNKNOWN. Record when it was requested on the
                # entry (persisted) so the next run's startup mirror
                # can bound the unprotected window: the exact moment
                # the stop stopped protecting is unknowable while its
                # status is unknown, so the true duration is bounded
                # by this request time and the confirmation time.
                record["stop_cancel_requested_at"] = stop_cancel_attempted_at
                skipped.append({
                    "symbol": symbol,
                    "stage": "exit_stop_cancel",
                    "reason": stop_reason,
                })
                log.info("skipping exit for %s: %s", symbol, stop_reason)
                continue
            stop_cancelled_at = datetime.now(timezone.utc).isoformat()
            # Re-derive the exit size from the leg's current position:
            # the stop may have sold shares between the buy-cancel
            # and the stop-cancel. Never sell more than is held.
            try:
                _paper_positions = broker.paper.positions() if broker.paper is not None else {}
            except Exception as exc:  # noqa: BLE001 - fail-closed
                skipped.append({
                    "symbol": symbol,
                    "stage": "exit_positions",
                    "reason": f"positions fetch failed: {exc}",
                })
                log.info("skipping exit for %s: positions fetch failed: %s", symbol, exc)
                continue
            _held = abs(float((_paper_positions or {}).get(symbol, {}).get("qty", 0.0) or 0.0))
            exit_qty = min(float(exit_qty), _held)
            held_at_exit = _held
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
        # B17.3: the stop's fills (shares it sold before the exit).
        # The stop's P&L is attributed to this exit in the same fields
        # the normal exit records.
        stop_sold_qty = sum(float(f.get("filled_qty") or 0.0) for f in stop_fills)
        stop_fill_price = None
        _priced = [f for f in stop_fills
                   if f.get("filled_avg_price") is not None and float(f.get("filled_qty") or 0.0) > 0]
        if _priced:
            _total = sum(float(f.get("filled_qty") or 0.0) for f in _priced)
            if _total > 0:
                stop_fill_price = sum(
                    float(f.get("filled_avg_price")) * float(f.get("filled_qty") or 0.0)
                    for f in _priced) / _total
        if exit_qty <= 0:
            # Nothing held: the entry never filled (its buy was
            # cancelled/expired before the exit), or the trailing stop
            # already sold the shares. No sell to submit — just close
            # the entry record below, attributing the stop's P&L.
            log.info(
                "exit for %s: nothing held (entry qty %s, filled 0); "
                "closing entry without a sell", symbol, entry_qty,
            )
        elif record["side"] == "buy":
            if manager is not None and manager.is_blocked(symbol, account="paper"):
                # Do not exit into an unresolved intent for this symbol;
                # leave the entry open. The BLOCKED intent was never
                # submitted; the exit DECISION is re-made next run,
                # and once the block clears, that run submits it for
                # the first time.
                log.info("skipping exit for %s: blocked by unresolved intent", symbol)
                continue
            paper_result = None
            exit_intent_id = None
            exit_attempts = int(record.get("exit_attempts") or 0)
            exit_inputs = {
                "symbol": symbol,
                "side": exit_side,
                "qty": float(exit_qty),
                "exit_reason": "signal_loss",
            }
            if exit_attempts > 0:
                # A re-derived exit after a rejected attempt: the
                # attempt number makes this a FRESH intent id (the
                # rejected intent's id is terminal and can never be
                # re-submitted). The first attempt omits the field,
                # so its id is unchanged.
                exit_inputs["exit_attempt"] = exit_attempts
            if manager is not None:
                exit_intent = _live_intent(exit_order, exit_inputs, today)
                exit_intent_id = exit_intent["intent_id"]
                paper_result = guardian.submit(
                    exit_order,
                    lambda o, _intent=exit_intent: _submit_paper_via_manager(o, _intent, manager, broker),
                )
            else:
                paper_result = guardian.submit(exit_order, broker.paper.submit)
            if paper_result is None:
                # The exit did NOT submit (broker failure, guardian
                # gate, or manager UNKNOWN) — but the trailing stop
                # was already cancelled above, so these shares are
                # now UNPROTECTED. Alert loudly (the monitored
                # critical path + the intent store), leave the entry
                # open, and do NOT close the decision/tax lot.
                # Detail 2: for an UNKNOWN exit, the next run retries
                # the LOOKUP only — the exit itself is never
                # re-submitted (exact-once); a new exit can only be
                # derived after the UNKNOWN intent resolves (lookup
                # adoption or human resolution).
                _alert_msg = (
                    f"exit for {symbol} did not submit after its trailing "
                    f"stop was cancelled; {exit_qty} shares are unprotected; "
                    "the next run retries the LOOKUP only — the exit itself is "
                    "never re-submitted")
                guardian.alerter.send("critical", _alert_msg)
                if manager is not None and exit_intent_id is not None:
                    manager.record_alert(exit_intent_id, "paper", symbol, _alert_msg)
                    # Durable exposure record (Detail 2): the exit's
                    # outcome is unknown; the next run retries the
                    # lookup only.
                    state.pending_exits[f"paper:{symbol}"] = {
                        "symbol": symbol, "account": "paper",
                        "exit_intent_id": exit_intent_id,
                        "qty": float(exit_qty), "filled_qty": 0.0,
                        "entry_price": record.get("entry_price"),
                        "decision_id": record.get("decision_id"),
                        "lot_id": record.get("lot_id"),
                        "opened_at": record.get("opened_at"),
                        "trail_percent": record.get("trail_percent"),
                        "exit_attempts": exit_attempts,
                        "exposure_started_at": stop_cancelled_at or datetime.now(timezone.utc).isoformat(),
                        "exposure_uncertain": stop_cancelled_at is None,
                        "exit_unknown": True,
                    }
                skipped.append({
                    "symbol": symbol,
                    "stage": "exit_unprotected",
                    "reason": _alert_msg,
                })
                log.info("skipping exit for %s: %s", symbol, _alert_msg)
                continue
            # B17.4: oversell detection after the exit submits (see
            # _check_exit_oversell: detection, NOT prevention).
            _oversell = _check_exit_oversell(
                broker, symbol, stop_fills, exit_qty, held_at_exit, guardian, manager)
            if _oversell:
                oversell_detections.append(_oversell)
                # The detection is also an audit-trail alert on the
                # exit intent (the alerts table), not just alerts.log.
                if manager is not None and exit_intent_id is not None:
                    manager.record_alert(
                        exit_intent_id, "paper", symbol, _oversell["note"])
            # B17.5: the live exit through the OrderManager when the
            # live leg is enabled (account="live"); while it is
            # disabled, the direct no-op call (no intent is created
            # for a disabled leg).
            live_result = None
            live_exit_intent_id = None
            if not broker.live.floor_tripped():
                if manager is not None and _live_leg_enabled(broker):
                    live_exit_inputs = dict(exit_inputs)
                    live_exit_intent = _live_intent(exit_order, live_exit_inputs, today, account="live")
                    live_exit_intent_id = live_exit_intent["intent_id"]
                    live_result = guardian.submit(
                        exit_order,
                        lambda o, _intent=live_exit_intent: _submit_live_via_manager(o, _intent, manager, broker),
                    )
                    if live_result is None:
                        # The live exit did not submit (UNKNOWN or
                        # rejected): alert and track it durably — the
                        # next run retries the LOOKUP only.
                        _live_alert = (
                            f"live exit for {symbol} did not submit; "
                            "the next run retries the LOOKUP only")
                        guardian.alerter.send("critical", _live_alert)
                        if manager is not None and live_exit_intent_id is not None:
                            manager.record_alert(live_exit_intent_id, "live", symbol, _live_alert)
                            state.pending_exits[f"live:{symbol}"] = {
                                "symbol": symbol, "account": "live",
                                "exit_intent_id": live_exit_intent_id,
                                "qty": float(exit_qty), "filled_qty": 0.0,
                                "entry_price": record.get("entry_price"),
                                "decision_id": record.get("decision_id"),
                                "lot_id": record.get("lot_id"),
                                "opened_at": record.get("opened_at"),
                                "trail_percent": record.get("trail_percent"),
                                "exit_attempts": exit_attempts,
                                "exposure_started_at": stop_cancelled_at or datetime.now(timezone.utc).isoformat(),
                                "exposure_uncertain": stop_cancelled_at is None,
                                "exit_unknown": True,
                            }
                else:
                    guardian.submit(exit_order, broker.live.submit)
            # Durable exposure record (Detail 2): the exit was
            # ACCEPTED but has not filled yet. The next run reads the
            # same record (held / exit-pending / protected, and the
            # unprotected count + exposure start time) from the state
            # file — it is not reset by the restart.
            if manager is not None and exit_intent_id is not None:
                state.pending_exits[f"paper:{symbol}"] = {
                    "symbol": symbol, "account": "paper",
                    "exit_intent_id": exit_intent_id,
                    "qty": float(exit_qty), "filled_qty": 0.0,
                    "entry_price": record.get("entry_price"),
                    "decision_id": record.get("decision_id"),
                    "lot_id": record.get("lot_id"),
                    "opened_at": record.get("opened_at"),
                    "trail_percent": record.get("trail_percent"),
                    "exit_attempts": exit_attempts,
                    "exposure_started_at": stop_cancelled_at or datetime.now(timezone.utc).isoformat(),
                    "exposure_uncertain": stop_cancelled_at is None,
                }
            if manager is not None and live_exit_intent_id is not None and live_result is not None:
                state.pending_exits[f"live:{symbol}"] = {
                    "symbol": symbol, "account": "live",
                    "exit_intent_id": live_exit_intent_id,
                    "qty": float(exit_qty), "filled_qty": 0.0,
                    "entry_price": record.get("entry_price"),
                    "decision_id": record.get("decision_id"),
                    "lot_id": record.get("lot_id"),
                    "opened_at": record.get("opened_at"),
                    "trail_percent": record.get("trail_percent"),
                    "exit_attempts": exit_attempts,
                    "exposure_started_at": stop_cancelled_at or datetime.now(timezone.utc).isoformat(),
                    "exposure_uncertain": stop_cancelled_at is None,
                }
        exit_price = _close_price_from_quote(quote, exit_side)
        holding_days = (
            datetime.now(timezone.utc) - datetime.fromisoformat(record["opened_at"])
        ).total_seconds() / 86400.0
        # B17.3: attribute the stop's fills to this exit in the same
        # fields the normal exit records: realized_pl = (stop fill
        # price - entry price) x stop-sold qty + (exit price - entry
        # price) x exit qty. When the stop sold shares but its fill
        # price could not be fetched, realized_pl is recorded as
        # UNKNOWN (None) — explicitly not 0 — and an alert fires.
        entry_price = float(record["entry_price"])
        if stop_sold_qty > 0 and stop_fill_price is not None:
            stop_pl = (stop_fill_price - entry_price) * stop_sold_qty
        elif stop_sold_qty > 0:
            stop_pl = None
        else:
            stop_pl = 0.0
        exit_pl = (exit_price - entry_price) * exit_qty
        if stop_pl is None:
            realized_pl = None
            exit_price_for_close = None if exit_qty <= 0 else exit_price
            _pl_alert = (
                f"exit for {symbol}: the trailing stop sold {stop_sold_qty} shares but "
                "its fill price could not be fetched; realized_pl is recorded as "
                "UNKNOWN (not 0)")
            guardian.alerter.send("critical", _pl_alert)
        else:
            realized_pl = stop_pl + exit_pl
            exit_price_for_close = stop_fill_price if exit_qty <= 0 else exit_price
        if stop_sold_qty > 0:
            stop_attribution.append({
                "symbol": symbol,
                "stop_sold_qty": round(stop_sold_qty, 4),
                "stop_filled_avg_price": stop_fill_price,
                "stop_realized_pl": None if stop_pl is None else round(stop_pl, 2),
                "exit_qty": round(float(exit_qty), 4),
                "exit_price": exit_price,
                "realized_pl": None if realized_pl is None else round(realized_pl, 2),
            })
        memory.close_decision(record["decision_id"], exit_price_for_close, realized_pl, holding_days)
        if record.get("lot_id"):
            if stop_sold_qty > 0 and stop_fill_price is not None:
                proceeds = stop_fill_price * stop_sold_qty + exit_price * exit_qty
            elif stop_sold_qty > 0:
                proceeds = None
            else:
                proceeds = exit_price * exit_qty
            memory.close_tax_lot(record["lot_id"], proceeds)
        state.open_entries.pop(symbol, None)
        state.expected_positions["paper"].pop(symbol, None)
        # B17.5: if the live exit was attempted but did not submit
        # (UNKNOWN — live_exit_intent_id exists, live_result is None),
        # the live leg still holds the shares and the live exit is
        # pending in pending_exits: KEEP the live expected position.
        # Popping it would make the end-of-run reconcile halt on live
        # drift (expected 0, actual 2 held). It is cleared when the
        # pending live exit resolves (FILLED → the live leg is flat;
        # CANCELLED / REJECTED → the entry is re-created with its
        # expected position). When the live leg is disabled there is
        # no live position, so the pop is a no-op either way.
        _live_exit_pending = (
            manager is not None and _live_leg_enabled(broker)
            and live_exit_intent_id is not None and live_result is None)
        if not _live_exit_pending:
            state.expected_positions["live"].pop(symbol, None)

    # Refresh the protection records for the exits still pending
    # (including ones created during this run), keeping the startup
    # records for exits that ended during this run. The summary's
    # protection list is the end-of-run view, not the startup view.
    if manager is not None and not dry_run:
        _fresh = _protection_records_now(broker, manager, state)
        protection = [p for p in protection
                      if f"{p.get('account')}:{p.get('symbol')}" not in _fresh]
        protection.extend(_fresh.values())

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
            "stop_sync": stop_sync,
            "done_for_day": done_for_day,
            "protection": protection,
            "stop_attribution": stop_attribution,
            "oversell_detections": oversell_detections,
        }
        _write_strategy_outputs(summary, cfg)
        return summary

    state.last_run_ts = datetime.now(timezone.utc).isoformat()
    state.save(cfg.paths.strategy_state_path)
    broker.save_state()

    reconcile_result = reconcile(broker, memory, cfg, guardian=guardian, manager=manager)
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
        "stop_sync": stop_sync,
        "done_for_day": done_for_day,
        "protection": protection,
        "stop_attribution": stop_attribution,
        "oversell_detections": oversell_detections,
        "summary": summary,
        "broker": status,
        "reconcile": reconcile_result,
    }
    _write_strategy_outputs(summary, cfg)
    return summary
