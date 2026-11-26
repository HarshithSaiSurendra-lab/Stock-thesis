"""
order_manager.py — B3 exact-once order lifecycle.

Acceptance criterion: one logical order produces at most one broker
order. A lookup can briefly return "not found" after acceptance — the
intent is kept pending and reconciled using the SAME client_order_id.
A timeout alone never creates a new order ID.

Design (user-adopted, Dec 2026):
1. Deterministic client_order_id: SHA-256 (first 32 hex chars) of a
   canonical string including design_id, trading_day, symbol, side,
   qty, order_type, limit_price and an inputs_hash. Same intent
   re-derived after restart/crash/race => same ID. Never UUID/clock.
2. SQLite intent store (not JSON): intents + fills tables. Claim
   atomically (INSERT OR IGNORE / single UPDATE); the loser reads the
   winner's intent and never submits. Claim lease (default 120s) —
   after expiry another worker may take over an UNKNOWN intent, still
   with the same ID.
3. submit(): persist+claim BEFORE any broker call. On success record
   broker_order_id. On explicit rejection (409/400) terminal
   REJECTED, no retry. On timeout/ambiguous error status UNKNOWN, do
   NOT create a new ID; reconcile() recovers it.
4. reconcile(): lookup by client_order_id; if found adopt it; if
   not-found, poll again after a grace period; if still not-found,
   RESUBMIT with the SAME client_order_id (Alpaca rejects a duplicate
   409 if the first was accepted but lookup missed it; if it never
   arrived, the resubmit creates it once).
5. Partial fills are deduplicated by fill_id; filled_qty is a
   monotonic cumulative, never double-counted.

This module never talks to a real broker. It takes place_order_fn /
lookup_fn callables, so tests use FakeBroker and the real Alpaca leg
can be wired later (client_order_id is passed through there).
ALLOW_LIVE is irrelevant here: no orders are placed by this module
itself, only through the injected callable, and the December B2 runner
uses only the fake broker.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Callable, Optional

CLAIM_LEASE_SECONDS = 120.0

TERMINAL_STATUSES = {"FILLED", "CANCELLED", "REJECTED"}
# Statuses that mean a broker order may exist (or may have existed).
RECOVERABLE_STATUSES = {"PENDING", "CLAIMED", "UNKNOWN", "SUBMITTED", "PARTIALLY_FILLED"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS intents (
    intent_id TEXT PRIMARY KEY,
    design_id TEXT NOT NULL,
    trading_day TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    order_type TEXT NOT NULL,
    limit_price REAL,
    inputs_hash TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    broker_order_id TEXT,
    filled_qty REAL NOT NULL DEFAULT 0,
    claimed_by TEXT,
    claim_expires_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS fills (
    fill_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL REFERENCES intents(intent_id),
    broker_order_id TEXT,
    qty REAL NOT NULL,
    price REAL,
    ts REAL
);
CREATE INDEX IF NOT EXISTS idx_intents_symbol_status
    ON intents (symbol, status);
"""


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def compute_inputs_hash(inputs) -> str:
    """Hash of the dated decision inputs (list of rows / any JSON)."""
    return hashlib.sha256(_canonical(inputs).encode()).hexdigest()[:32]


def derive_client_order_id(
    design_id: str,
    trading_day: str,
    symbol: str,
    side: str,
    qty,
    order_type: str = "market",
    limit_price=None,
    inputs_hash: str = "",
) -> str:
    canonical = _canonical(
        {
            "design_id": design_id,
            "trading_day": trading_day,
            "symbol": symbol,
            "side": side,
            "qty": float(qty),
            "order_type": order_type,
            "limit_price": None if limit_price is None else float(limit_price),
            "inputs_hash": inputs_hash or "",
        }
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def build_intent(
    design_id: str,
    trading_day: str,
    symbol: str,
    side: str,
    qty,
    order_type: str = "market",
    limit_price=None,
    inputs_hash: str = "",
) -> dict:
    """Build the intent dict (including its deterministic ID) for an order."""
    return {
        "intent_id": derive_client_order_id(
            design_id, trading_day, symbol, side, qty, order_type, limit_price, inputs_hash
        ),
        "design_id": design_id,
        "trading_day": trading_day,
        "symbol": symbol,
        "side": side,
        "qty": float(qty),
        "order_type": order_type,
        "limit_price": None if limit_price is None else float(limit_price),
        "inputs_hash": inputs_hash or "",
        "status": "PENDING",
        "broker_order_id": None,
        "filled_qty": 0.0,
    }


def _is_explicit_rejection(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(
        token in text
        for token in ("409", "duplicate", "400", "validation", "rejected", "invalid order")
    )


def _is_ambiguous(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    text = str(exc).lower()
    return any(token in text for token in ("timeout", "timed out", "ambiguous", "connection", "temporarily"))


class DuplicateClientOrderIdError(RuntimeError):
    """Raised when a broker rejects a submit as a 409 duplicate client_order_id."""


class OrderManager:
    """Exact-once order lifecycle over a SQLite intent store."""

    def __init__(
        self,
        db_path: str | Path,
        claim_lease_seconds: float = CLAIM_LEASE_SECONDS,
        time_fn: Callable[[], float] | None = None,
    ):
        self.db_path = Path(db_path)
        if self.db_path.parent:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.claim_lease_seconds = claim_lease_seconds
        self._now = time_fn or time.time
        self._conn = sqlite3.connect(str(self.db_path), timeout=10.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        with self._conn:
            self._conn.executescript(SCHEMA)

    # -- low-level ------------------------------------------------------
    def _fetch(self, intent_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM intents WHERE intent_id = ?", (intent_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_intent(self, intent_id: str) -> Optional[dict]:
        return self._fetch(intent_id)

    def _update(self, intent_id: str, **fields) -> None:
        fields["updated_at"] = self._now()
        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self._conn:
            self._conn.execute(
                f"UPDATE intents SET {assignments} WHERE intent_id = ?",
                (*fields.values(), intent_id),
            )

    def _insert_intent(self, intent: dict, claimed_by: str) -> bool:
        """INSERT OR IGNORE; True if this call created the row."""
        now = self._now()
        with self._conn:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO intents
                (intent_id, design_id, trading_day, symbol, side, qty,
                 order_type, limit_price, inputs_hash, status,
                 broker_order_id, filled_qty, claimed_by, claim_expires_at,
                 created_at, updated_at, attempts, last_error)
                VALUES (?,?,?,?,?,?,?,?,?,'PENDING',?,0,?,?,?, ?,0,'')""",
                (
                    intent["intent_id"],
                    intent["design_id"],
                    intent["trading_day"],
                    intent["symbol"],
                    intent["side"],
                    intent["qty"],
                    intent["order_type"],
                    intent["limit_price"],
                    intent["inputs_hash"],
                    intent.get("broker_order_id"),
                    claimed_by,
                    now + self.claim_lease_seconds,
                    now,
                    now,
                ),
            )
            return cur.rowcount == 1

    def _try_claim(self, intent_id: str, claimed_by: str) -> bool:
        """Atomically claim a PENDING intent, an expired CLAIMED one,
        or one already held by this same worker (re-entrant)."""
        now = self._now()
        with self._conn:
            cur = self._conn.execute(
                """UPDATE intents SET status='CLAIMED', claimed_by=?,
                       claim_expires_at=?, updated_at=?
                   WHERE intent_id=? AND (
                       status='PENDING'
                       OR (status='CLAIMED' AND claim_expires_at<=?)
                       OR (status='CLAIMED' AND claimed_by=?)
                   )""",
                (claimed_by, now + self.claim_lease_seconds, now, intent_id, now, claimed_by),
            )
            return cur.rowcount == 1

    def _adopt_broker_order(self, intent: dict, broker_order: dict) -> dict:
        broker_id = str(broker_order.get("id") or broker_order.get("broker_order_id") or "")
        status = str(broker_order.get("status") or "submitted").lower()
        if status in {"filled", "fully_filled"}:
            new_status = "FILLED"
        elif status in {"partially_filled", "partially filled"}:
            new_status = "PARTIALLY_FILLED"
        elif status in {"cancelled", "canceled", "rejected"}:
            new_status = "CANCELLED" if "cancel" in status else "REJECTED"
        else:
            new_status = "SUBMITTED"
        filled = broker_order.get("filled_qty")
        fields: dict = {
            "status": new_status,
            "broker_order_id": broker_id or intent.get("broker_order_id"),
            "claimed_by": None,
            "claim_expires_at": None,
        }
        if filled is not None:
            try:
                fields["filled_qty"] = max(float(intent.get("filled_qty") or 0.0), float(filled))
            except (TypeError, ValueError):
                pass
        self._update(intent["intent_id"], **fields)
        return self._fetch(intent["intent_id"])

    # -- submit / reconcile ---------------------------------------------
    def submit(
        self,
        intent: dict,
        place_order_fn: Callable[[dict], dict],
        lookup_fn: Callable[[str], Optional[dict]] | None = None,
        claimed_by: str = "worker-1",
        sleep_fn: Callable[[float], None] | None = None,
        grace_seconds: float = 0.0,
        max_polls: int = 3,
    ) -> dict:
        """Submit one logical order exactly once.

        Returns the persisted intent (with broker_order_id when known).
        Never creates a second client_order_id for the same intent.
        """
        sleep = sleep_fn or time.sleep
        intent_id = intent["intent_id"]
        self._insert_intent(intent, claimed_by)

        # Fast path: already terminal / already submitted.
        current = self._fetch(intent_id)
        if current is None:  # pragma: no cover - insert just succeeded
            raise RuntimeError(f"intent {intent_id} vanished after insert")
        if current["status"] in TERMINAL_STATUSES:
            return current
        if current["status"] in {"SUBMITTED", "PARTIALLY_FILLED", "FILLED"} and current.get("broker_order_id"):
            return current
        if current["status"] == "UNKNOWN":
            return self.reconcile(
                current, lookup_fn, place_order_fn,
                grace_seconds=grace_seconds, max_polls=max_polls, sleep_fn=sleep,
            )

        # Claim it; if another worker holds the claim, wait for it to
        # resolve, then return whatever it produced — never submit twice.
        if not self._try_claim(intent_id, claimed_by):
            for _ in range(200):
                sleep(0.01)
                current = self._fetch(intent_id)
                if current is None:
                    break
                if current["status"] in TERMINAL_STATUSES:
                    return current
                if current["status"] in {"SUBMITTED", "PARTIALLY_FILLED", "FILLED"} and current.get("broker_order_id"):
                    return current
                if current["status"] == "UNKNOWN":
                    return self.reconcile(
                        current, lookup_fn, place_order_fn,
                        grace_seconds=grace_seconds, max_polls=max_polls, sleep_fn=sleep,
                    )
                if current["status"] == "PENDING":
                    break  # claim expired / released; try to claim below
            if not self._try_claim(intent_id, claimed_by):
                # Still held by a live worker; return the intent as-is
                # rather than submitting a duplicate.
                return self._fetch(intent_id)

        order = {
            "symbol": intent["symbol"],
            "side": intent["side"],
            "qty": intent["qty"],
            "order_type": intent["order_type"],
            "client_order_id": intent_id,
        }
        if intent.get("limit_price") is not None:
            order["limit_price"] = intent["limit_price"]

        try:
            result = place_order_fn(order)
        except Exception as exc:  # noqa: BLE001 - classify, then recover
            self._update(intent_id, attempts=(self._fetch(intent_id) or {}).get("attempts", 0) + 1,
                         last_error=str(exc))
            if _is_explicit_rejection(exc):
                # Broker explicitly rejected it. If it is a 409 duplicate,
                # the order may still exist — try the lookup once; only
                # mark REJECTED when the lookup confirms nothing exists.
                if lookup_fn is not None and ("409" in str(exc) or "duplicate" in str(exc).lower()):
                    try:
                        found = lookup_fn(intent_id)
                    except Exception:
                        found = None
                    if found:
                        return self._adopt_broker_order(self._fetch(intent_id), found)
                self._update(intent_id, status="REJECTED", claimed_by=None, claim_expires_at=None)
                return self._fetch(intent_id)
            # Ambiguous: timeout / connection loss. The broker may have
            # accepted it. Mark UNKNOWN and reconcile with the same ID.
            self._update(intent_id, status="UNKNOWN", claimed_by=None, claim_expires_at=None)
            if lookup_fn is None and place_order_fn is None:
                return self._fetch(intent_id)
            return self.reconcile(
                self._fetch(intent_id), lookup_fn, place_order_fn,
                grace_seconds=grace_seconds, max_polls=max_polls, sleep_fn=sleep,
            )

        if result is None:
            # place_order_fn returned None without raising: treat as
            # ambiguous (nothing was confirmed).
            self._update(intent_id, status="UNKNOWN", claimed_by=None, claim_expires_at=None)
            return self.reconcile(
                self._fetch(intent_id), lookup_fn, place_order_fn,
                grace_seconds=grace_seconds, max_polls=max_polls, sleep_fn=sleep,
            )
        return self._adopt_broker_order(self._fetch(intent_id), result)

    def reconcile(
        self,
        intent: dict,
        lookup_fn: Callable[[str], Optional[dict]] | None = None,
        place_order_fn: Callable[[dict], dict] | None = None,
        grace_seconds: float = 5.0,
        max_polls: int = 3,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> dict:
        """Recover an UNKNOWN / PENDING intent using the same client_order_id.

        Lookup first (adopt if found), poll again after the grace
        period, then — only if still not found — resubmit with the SAME
        client_order_id. The broker rejects a duplicate 409 if the
        first submit was accepted but lookup missed it.
        """
        sleep = sleep_fn or time.sleep
        intent_id = intent["intent_id"]
        current = self._fetch(intent_id) or intent
        if current["status"] in TERMINAL_STATUSES:
            return current
        if current["status"] in {"SUBMITTED", "PARTIALLY_FILLED", "FILLED"} and current.get("broker_order_id"):
            return current

        found = None
        if lookup_fn is not None:
            for poll in range(max(1, max_polls)):
                try:
                    found = lookup_fn(intent_id)
                except Exception:
                    found = None
                if found:
                    break
                if poll < max_polls - 1:
                    sleep(grace_seconds)
        if found:
            return self._adopt_broker_order(current, found)

        # Still not found after the grace polls. Resubmit with the SAME
        # ID — never a new one. If the broker did accept the first
        # submit, it rejects this as a duplicate and we adopt via lookup.
        if place_order_fn is None:
            return self._fetch(intent_id)
        order = {
            "symbol": current["symbol"],
            "side": current["side"],
            "qty": current["qty"],
            "order_type": current["order_type"],
            "client_order_id": intent_id,
        }
        if current.get("limit_price") is not None:
            order["limit_price"] = current["limit_price"]
        try:
            result = place_order_fn(order)
        except Exception as exc:  # noqa: BLE001
            self._update(intent_id, attempts=(self._fetch(intent_id) or {}).get("attempts", 0) + 1,
                         last_error=str(exc))
            if _is_explicit_rejection(exc) and lookup_fn is not None:
                try:
                    found = lookup_fn(intent_id)
                except Exception:
                    found = None
                if found:
                    return self._adopt_broker_order(self._fetch(intent_id), found)
                self._update(intent_id, status="REJECTED", claimed_by=None, claim_expires_at=None)
                return self._fetch(intent_id)
            # Still ambiguous — stay UNKNOWN for the next reconcile pass.
            self._update(intent_id, status="UNKNOWN", claimed_by=None, claim_expires_at=None)
            return self._fetch(intent_id)
        if result is None:
            self._update(intent_id, status="UNKNOWN", claimed_by=None, claim_expires_at=None)
            return self._fetch(intent_id)
        return self._adopt_broker_order(self._fetch(intent_id), result)

    def reconcile_all_unknowns(
        self,
        lookup_fn: Callable[[str], Optional[dict]] | None = None,
        place_order_fn: Callable[[dict], dict] | None = None,
        symbol: str | None = None,
        grace_seconds: float = 0.0,
        max_polls: int = 3,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> list[dict]:
        """Startup reconcile: recover every PENDING / expired-CLAIMED /
        UNKNOWN intent (optionally only for one symbol) before any new
        order for that symbol is accepted."""
        now = self._now()
        query = """SELECT * FROM intents WHERE (
            status='UNKNOWN' OR status='PENDING'
            OR (status='CLAIMED' AND claim_expires_at<=?)
        )"""
        params: list = [now]
        if symbol is not None:
            query += " AND symbol=?"
            params.append(symbol)
        rows = self._conn.execute(query, params).fetchall()
        recovered = []
        for row in rows:
            recovered.append(
                self.reconcile(
                    dict(row), lookup_fn, place_order_fn,
                    grace_seconds=grace_seconds, max_polls=max_polls, sleep_fn=sleep_fn,
                )
            )
        return recovered

    # -- fills ------------------------------------------------------------
    def apply_fill_update(self, intent_id: str, fill: dict) -> tuple[bool, float, float]:
        """Apply one fill update exactly once.

        fill = {fill_id, qty, cumulative_filled_qty (optional), price,
                broker_order_id (optional), ts (optional)}.
        Returns (applied, filled_qty, remaining_qty). A replay of an
        already-seen fill_id returns applied=False with quantities
        unchanged. A cumulative snapshot that does not advance the
        total is likewise a no-op. filled_qty only ever moves forward.
        """
        intent = self._fetch(intent_id)
        if intent is None:
            raise KeyError(f"unknown intent {intent_id}")
        fill_id = str(fill["fill_id"])
        existing = self._conn.execute(
            "SELECT 1 FROM fills WHERE fill_id=?", (fill_id,)
        ).fetchone()
        if existing:
            return False, float(intent["filled_qty"]), float(intent["qty"]) - float(intent["filled_qty"])

        filled = float(intent["filled_qty"])
        qty = float(intent["qty"])
        cumulative = fill.get("cumulative_filled_qty")
        if cumulative is not None:
            try:
                cumulative = float(cumulative)
            except (TypeError, ValueError):
                cumulative = None
        if cumulative is not None:
            if cumulative <= filled:
                return False, filled, qty - filled
            delta = cumulative - filled
            new_filled = min(cumulative, qty)
        else:
            delta = float(fill.get("qty") or 0.0)
            if delta <= 0:
                return False, filled, qty - filled
            new_filled = min(filled + delta, qty)

        with self._conn:
            self._conn.execute(
                "INSERT INTO fills (fill_id, intent_id, broker_order_id, qty, price, ts)"
                " VALUES (?,?,?,?,?,?)",
                (
                    fill_id,
                    intent_id,
                    fill.get("broker_order_id") or intent.get("broker_order_id"),
                    delta,
                    fill.get("price"),
                    fill.get("ts") or self._now(),
                ),
            )
        status = "FILLED" if new_filled >= qty else "PARTIALLY_FILLED"
        self._update(intent_id, filled_qty=new_filled, status=status)
        return True, new_filled, qty - new_filled

    def get_fill_state(self, intent_id: str) -> dict:
        intent = self._fetch(intent_id)
        if intent is None:
            raise KeyError(f"unknown intent {intent_id}")
        fills = [
            dict(r)
            for r in self._conn.execute(
                "SELECT * FROM fills WHERE intent_id=? ORDER BY ts", (intent_id,)
            ).fetchall()
        ]
        return {
            "intent_id": intent_id,
            "status": intent["status"],
            "qty": float(intent["qty"]),
            "filled_qty": float(intent["filled_qty"]),
            "remaining_qty": float(intent["qty"]) - float(intent["filled_qty"]),
            "fills": fills,
        }

    def close(self) -> None:
        self._conn.close()
