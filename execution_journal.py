from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    trading_day TEXT,
    session TEXT,
    status TEXT,
    phase TEXT,
    action TEXT,
    symbol TEXT,
    stage TEXT,
    reason TEXT,
    decision_score REAL,
    signal_score REAL,
    trend_quality REAL,
    after_hours_score REAL,
    after_hours_move_pct REAL,
    relative_strength_63 REAL,
    sector_benchmark TEXT,
    sector_relative_strength_63 REAL,
    momentum_126_21 REAL,
    rvol_20 REAL,
    spread_pct REAL,
    dollar_volume REAL,
    last_price REAL,
    bid REAL,
    ask REAL,
    midpoint REAL,
    quote_age_seconds REAL,
    target_notional REAL,
    intended_notional REAL,
    limit_price REAL,
    qty REAL,
    order_type TEXT,
    side TEXT,
    time_in_force TEXT,
    extended_hours INTEGER,
    order_id TEXT,
    order_status TEXT,
    filled_qty REAL,
    avg_fill_price REAL,
    expected_midpoint REAL,
    slippage_bps REAL,
    time_to_fill_seconds REAL,
    cancel_reason TEXT,
    payload_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_execution_events_day_session
    ON execution_events (trading_day, session, action);
CREATE INDEX IF NOT EXISTS idx_execution_events_symbol
    ON execution_events (symbol, trading_day);
CREATE INDEX IF NOT EXISTS idx_execution_events_reason
    ON execution_events (stage, reason);
"""


EVENT_COLUMNS = (
    "ts",
    "trading_day",
    "session",
    "status",
    "phase",
    "action",
    "symbol",
    "stage",
    "reason",
    "decision_score",
    "signal_score",
    "trend_quality",
    "after_hours_score",
    "after_hours_move_pct",
    "relative_strength_63",
    "sector_benchmark",
    "sector_relative_strength_63",
    "momentum_126_21",
    "rvol_20",
    "spread_pct",
    "dollar_volume",
    "last_price",
    "bid",
    "ask",
    "midpoint",
    "quote_age_seconds",
    "target_notional",
    "intended_notional",
    "limit_price",
    "qty",
    "order_type",
    "side",
    "time_in_force",
    "extended_hours",
    "order_id",
    "order_status",
    "filled_qty",
    "avg_fill_price",
    "expected_midpoint",
    "slippage_bps",
    "time_to_fill_seconds",
    "cancel_reason",
    "payload_json",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(value) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _safe_int_bool(value) -> Optional[int]:
    if value is None:
        return None
    return 1 if bool(value) else 0


def _payload_json(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, default=str)


def _midpoint_from_quote(quote: dict) -> Optional[float]:
    bid = _safe_float(quote.get("bid"))
    ask = _safe_float(quote.get("ask"))
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2


class ExecutionJournal:
    """SQLite journal for reconstructing strategy decisions and execution quality."""

    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        if self.db_path.parent:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def log_event(self, event: dict) -> int:
        payload = dict(event)
        payload.setdefault("ts", _utc_now())
        payload.setdefault("payload_json", _payload_json(event))
        values = [payload.get(column) for column in EVENT_COLUMNS]
        placeholders = ", ".join("?" for _ in EVENT_COLUMNS)
        columns = ", ".join(EVENT_COLUMNS)
        with self._connect() as conn:
            cursor = conn.execute(
                f"INSERT INTO execution_events ({columns}) VALUES ({placeholders})",
                values,
            )
            return int(cursor.lastrowid)

    def log_summary(self, summary: dict, phase: Optional[str] = None) -> int:
        trading_day = summary.get("date")
        session = summary.get("session") or summary.get("router", {}).get("session") or "regular"
        status = summary.get("status")
        phase = phase or _phase_from_status(status)
        run_id = self.log_event(
            {
                "trading_day": trading_day,
                "session": session,
                "status": status,
                "phase": phase,
                "action": "run_summary",
                "reason": summary.get("reason"),
                "payload_json": _payload_json(
                    {
                        "budget": summary.get("budget"),
                        "regime": summary.get("regime"),
                        "breadth": summary.get("breadth"),
                        "skip_summary": summary.get("skip_summary"),
                        "router": summary.get("router"),
                    }
                ),
            }
        )
        selected = set(summary.get("selected", []))
        orders_by_symbol = {
            order.get("symbol"): order
            for order in summary.get("orders", [])
            if order.get("symbol")
        }
        for item in summary.get("decision_report", []) or summary.get("candidates", []):
            symbol = item.get("symbol")
            action = "selected" if symbol in selected else "candidate"
            if symbol in orders_by_symbol:
                action = "order_candidate"
            self.log_event(
                _event_from_decision_item(
                    item,
                    trading_day=trading_day,
                    session=session,
                    status=status,
                    phase=phase,
                    action=action,
                )
            )
        for order in summary.get("orders", []):
            self.log_event(
                _event_from_order(
                    order,
                    trading_day=trading_day,
                    session=session,
                    status=status,
                    phase=phase,
                )
            )
        for skipped in summary.get("skipped", []):
            self.log_event(
                _event_from_skip(
                    skipped,
                    trading_day=trading_day,
                    session=session,
                    status=status,
                    phase=phase,
                )
            )
        return run_id

    def day_summary(self, trading_day: Optional[str] = None, session: Optional[str] = None) -> dict:
        clauses = []
        params = []
        if trading_day:
            clauses.append("trading_day = ?")
            params.append(trading_day)
        if session:
            clauses.append("session = ?")
            params.append(session)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            counts = {
                row["action"]: row["count"]
                for row in conn.execute(
                    f"SELECT action, COUNT(*) AS count FROM execution_events {where} GROUP BY action",
                    params,
                )
            }
            skip_rows = conn.execute(
                f"""
                SELECT stage, reason, COUNT(*) AS count
                FROM execution_events
                {where + (' AND' if where else 'WHERE')} action = 'skipped'
                GROUP BY stage, reason
                ORDER BY count DESC, stage, reason
                """,
                params,
            ).fetchall()
            metrics = conn.execute(
                f"""
                SELECT
                    AVG(spread_pct) AS avg_spread_pct,
                    AVG(quote_age_seconds) AS avg_quote_age_seconds,
                    AVG(slippage_bps) AS avg_slippage_bps
                FROM execution_events
                {where}
                """,
                params,
            ).fetchone()
        return {
            "trading_day": trading_day,
            "session": session,
            "counts": counts,
            "skip_summary": [
                {
                    "stage": row["stage"],
                    "reason": row["reason"],
                    "count": row["count"],
                }
                for row in skip_rows
            ],
            "avg_spread_pct": metrics["avg_spread_pct"],
            "avg_quote_age_seconds": metrics["avg_quote_age_seconds"],
            "avg_slippage_bps": metrics["avg_slippage_bps"],
        }


def _phase_from_status(status: Optional[str]) -> str:
    text = str(status or "")
    if "dry_run" in text:
        return "dry_run"
    if "disabled" in text or "blocked" in text or "halted" in text or "closed" in text:
        return "blocked"
    return "paper_order"


def _base_event(
    *,
    trading_day: Optional[str],
    session: str,
    status: Optional[str],
    phase: str,
    action: str,
    symbol: Optional[str] = None,
) -> dict:
    return {
        "trading_day": trading_day,
        "session": session,
        "status": status,
        "phase": phase,
        "action": action,
        "symbol": symbol,
    }


def _quote_fields(payload: dict) -> dict:
    quote = payload.get("quote") if isinstance(payload.get("quote"), dict) else payload
    midpoint = _midpoint_from_quote(quote)
    return {
        "bid": _safe_float(quote.get("bid")),
        "ask": _safe_float(quote.get("ask")),
        "midpoint": midpoint,
        "spread_pct": _safe_float(quote.get("spread_pct")),
        "quote_age_seconds": _safe_float(
            quote.get("quote_age_seconds")
            if quote.get("quote_age_seconds") is not None
            else quote.get("age_seconds")
        ),
    }


def _event_from_decision_item(
    item: dict,
    *,
    trading_day: Optional[str],
    session: str,
    status: Optional[str],
    phase: str,
    action: str,
) -> dict:
    event = _base_event(
        trading_day=trading_day,
        session=session,
        status=status,
        phase=phase,
        action=action,
        symbol=item.get("symbol"),
    )
    event.update(
        {
            "decision_score": _safe_float(item.get("decision_score")),
            "signal_score": _safe_float(item.get("signal_score") or item.get("score")),
            "trend_quality": _safe_float(item.get("trend_quality")),
            "after_hours_score": _safe_float(item.get("after_hours_score")),
            "after_hours_move_pct": _safe_float(item.get("after_hours_move_pct")),
            "relative_strength_63": _safe_float(item.get("relative_strength_63")),
            "sector_benchmark": item.get("sector_benchmark"),
            "sector_relative_strength_63": _safe_float(item.get("sector_relative_strength_63")),
            "momentum_126_21": _safe_float(item.get("momentum_126_21")),
            "rvol_20": _safe_float(item.get("rvol_20")),
            "spread_pct": _safe_float(item.get("spread_pct")),
            "dollar_volume": _safe_float(item.get("dollar_volume")),
            "last_price": _safe_float(item.get("last_price")),
            "quote_age_seconds": _safe_float(item.get("quote_age_seconds")),
            "payload_json": _payload_json(item),
        }
    )
    return event


def _event_from_order(
    order_preview: dict,
    *,
    trading_day: Optional[str],
    session: str,
    status: Optional[str],
    phase: str,
) -> dict:
    order = order_preview.get("order", {})
    limit_price = _safe_float(order.get("limit_price"))
    midpoint = _safe_float(order_preview.get("expected_midpoint"))
    avg_fill = _safe_float(order_preview.get("avg_fill_price"))
    slippage_bps = _safe_float(order_preview.get("slippage_bps"))
    if slippage_bps is None and midpoint and avg_fill:
        side = order.get("side", "buy")
        slippage = avg_fill - midpoint if side == "buy" else midpoint - avg_fill
        slippage_bps = slippage / midpoint * 10_000
    event = _base_event(
        trading_day=trading_day,
        session=session,
        status=status,
        phase=phase,
        action="order_preview" if order_preview.get("dry_run") else "order_submitted",
        symbol=order_preview.get("symbol") or order.get("symbol"),
    )
    event.update(
        {
            "decision_score": _safe_float(order_preview.get("decision_score")),
            "after_hours_score": _safe_float(order_preview.get("after_hours_score")),
            "after_hours_move_pct": _safe_float(order_preview.get("after_hours_move_pct")),
            "quote_age_seconds": _safe_float(order_preview.get("quote_age_seconds")),
            "target_notional": _safe_float(order_preview.get("target_notional")),
            "intended_notional": _safe_float(order_preview.get("intended_notional")),
            "limit_price": limit_price,
            "qty": _safe_float(order.get("qty")),
            "order_type": order.get("order_type"),
            "side": order.get("side"),
            "time_in_force": order.get("time_in_force"),
            "extended_hours": _safe_int_bool(order.get("extended_hours")),
            "order_id": order_preview.get("order_id"),
            "order_status": order_preview.get("order_status"),
            "filled_qty": _safe_float(order_preview.get("filled_qty")),
            "avg_fill_price": avg_fill,
            "expected_midpoint": midpoint,
            "slippage_bps": slippage_bps,
            "time_to_fill_seconds": _safe_float(order_preview.get("time_to_fill_seconds")),
            "cancel_reason": order_preview.get("cancel_reason"),
            "payload_json": _payload_json(order_preview),
        }
    )
    return event


def _event_from_skip(
    skipped: dict,
    *,
    trading_day: Optional[str],
    session: str,
    status: Optional[str],
    phase: str,
) -> dict:
    event = _base_event(
        trading_day=trading_day,
        session=session,
        status=status,
        phase=phase,
        action="skipped",
        symbol=skipped.get("symbol"),
    )
    event.update(
        {
            "stage": skipped.get("stage"),
            "reason": skipped.get("reason"),
            "target_notional": _safe_float(skipped.get("target_notional")),
            "intended_notional": _safe_float(skipped.get("intended_notional")),
            "last_price": _safe_float(skipped.get("price") or skipped.get("last_price")),
            "after_hours_move_pct": _safe_float(skipped.get("after_hours_move_pct")),
            "payload_json": _payload_json(skipped),
        }
    )
    event.update(_quote_fields(skipped))
    return event
