"""
order_manager.py — B3 exact-once order lifecycle.

Acceptance criterion: one logical order produces at most one broker
order. A lookup can briefly return "not found" after acceptance — the
intent is kept pending and reconciled using the SAME client_order_id.
A timeout alone never creates a new order ID.

Design (user-adopted, Dec 2026; corrected Nov 26, 2026 — see point 4):
1. Deterministic client_order_id: SHA-256 (first 32 hex chars) of a
   canonical string including design_id, trading_day, symbol, side,
   qty, order_type, limit_price and an inputs_hash. Same intent
   re-derived after restart/crash/race => same ID. Never UUID/clock.
2. SQLite intent store (not JSON): intents + fills + alerts tables.
   Claim atomically (INSERT OR IGNORE / single UPDATE); the loser
   reads the winner's intent and never submits. Claim lease (default
   120s) — after expiry another worker may take over, still with the
   same ID, but see point 4: takeover never authorizes a second
   submission attempt.
3. submit(): persist+claim BEFORE any broker call. `attempts` is
   incremented and persisted BEFORE place_order_fn is called, so a
   crash mid-call still leaves attempts >= 1 on disk. On explicit
   non-duplicate rejection (400/validation) terminal REJECTED, no
   retry. On a 409/duplicate conflict, reconcile the existing order
   first (lookup with grace polls); only classify as REJECTED after
   the lookup conclusively shows no order — and a 409 never becomes
   REJECTED on an inconclusive lookup. On timeout/ambiguous error
   status UNKNOWN, do NOT create a new ID; reconcile() recovers it.
4. reconcile() is LOOKUP-ONLY. It polls lookup_fn by
   client_order_id, adopts the order if found, and otherwise leaves
   the intent UNKNOWN, records a persistent alert, and blocks
   conflicting orders for that (account, symbol) until the intent
   resolves. It NEVER calls place_order_fn — there is no resubmit
   path anywhere in this module. A restart, a lease expiry, or a
   404 "not found" never authorizes a second submission attempt:
   exactly one submission attempt per logical order, ever. If the
   broker accepted the first attempt but the lookup stays
   inconclusive, the intent stays UNKNOWN and flagged; a human (or
   a later lookup that finds it) resolves it. The earlier resubmit-
   same-ID behaviour was removed Nov 26, 2026: the broker deduping
   a second attempt to one order did not make the second attempt
   acceptable.
5. Partial fills are deduplicated by fill_id; filled_qty is a
   monotonic cumulative, never double-counted.
6. Blocking: while an intent is UNKNOWN for an (account, symbol),
   submit() refuses any OTHER intent for that (account, symbol) —
   it persists the new intent as BLOCKED and never calls
   place_order_fn for it. Once the blocking intent resolves
   (SUBMITTED/FILLED/REJECTED/CANCELLED), a later submit of the
   BLOCKED intent proceeds normally.

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
    account TEXT NOT NULL DEFAULT 'paper',
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
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id TEXT NOT NULL REFERENCES intents(intent_id),
    account TEXT NOT NULL DEFAULT 'paper',
    symbol TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at REAL NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_intents_symbol_status
    ON intents (symbol, status);
CREATE INDEX IF NOT EXISTS idx_intents_account_symbol_status
    ON intents (account, symbol, status);
CREATE INDEX IF NOT EXISTS idx_alerts_symbol_resolved
    ON alerts (symbol, resolved);
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
    account: str = "paper",
) -> dict:
    """Build the intent dict (including its deterministic ID) for an order.

    `account` scopes the symbol block (point 6 in the module docstring);
    it is NOT part of the client_order_id, so re-deriving the same
    logical order still yields the same ID."""
    return {
        "intent_id": derive_client_order_id(
            design_id, trading_day, symbol, side, qty, order_type, limit_price, inputs_hash
        ),
        "design_id": design_id,
        "trading_day": trading_day,
        "symbol": symbol,
        "account": account,
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
        # Migration for DB files created before the `account` column
        # and the `alerts` table existed (Nov 26, 2026 correction):
        # CREATE TABLE IF NOT EXISTS does not add columns to an
        # existing table, so add it explicitly when missing.
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(intents)").fetchall()}
        if "account" not in cols:
            with self._conn:
                self._conn.execute(
                    "ALTER TABLE intents ADD COLUMN account TEXT NOT NULL DEFAULT 'paper'"
                )

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
                (intent_id, design_id, trading_day, symbol, account, side, qty,
                 order_type, limit_price, inputs_hash, status,
                 broker_order_id, filled_qty, claimed_by, claim_expires_at,
                 created_at, updated_at, attempts, last_error)
                VALUES (?,?,?,?,?,?,?,?,?,?,'PENDING',?,0,?,?,?, ?,0,'')""",
                (
                    intent["intent_id"],
                    intent["design_id"],
                    intent["trading_day"],
                    intent["symbol"],
                    intent.get("account") or "paper",
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

    # -- alerts / blocking ----------------------------------------------
    def _record_alert(self, intent_id: str, account: str, symbol: str, reason: str) -> None:
        """Persist an unresolved-intent alert (deduplicated: one open
        alert per intent+reason). The alert is the audit trail for
        'lookup stayed inconclusive — no second submission was made'."""
        existing = self._conn.execute(
            "SELECT 1 FROM alerts WHERE intent_id=? AND reason=? AND resolved=0",
            (intent_id, reason),
        ).fetchone()
        if existing:
            return
        with self._conn:
            self._conn.execute(
                "INSERT INTO alerts (intent_id, account, symbol, reason, created_at, resolved)"
                " VALUES (?,?,?,?,?,0)",
                (intent_id, account or "paper", symbol, reason, self._now()),
            )

    def get_alerts(self, intent_id: str | None = None, unresolved_only: bool = True) -> list[dict]:
        query = "SELECT * FROM alerts"
        params: list = []
        clauses = []
        if intent_id is not None:
            clauses.append("intent_id=?")
            params.append(intent_id)
        if unresolved_only:
            clauses.append("resolved=0")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at"
        return [dict(r) for r in self._conn.execute(query, params).fetchall()]

    def _resolve_alerts(self, intent_id: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE alerts SET resolved=1 WHERE intent_id=? AND resolved=0", (intent_id,)
            )

    def blocked_symbols(self, account: str = "paper") -> list[str]:
        """Symbols with an UNKNOWN intent for this account: conflicting
        orders for them are refused until the intent resolves."""
        rows = self._conn.execute(
            "SELECT DISTINCT symbol FROM intents WHERE account=? AND status='UNKNOWN'",
            (account,),
        ).fetchall()
        return sorted(r[0] for r in rows)

    def is_blocked(self, symbol: str, account: str = "paper") -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM intents WHERE account=? AND symbol=? AND status='UNKNOWN' LIMIT 1",
            (account, symbol),
        ).fetchone()
        return row is not None

    def _blocking_intent(self, symbol: str, account: str, exclude_intent_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM intents WHERE account=? AND symbol=? AND status='UNKNOWN'"
            " AND intent_id!=? LIMIT 1",
            (account, symbol, exclude_intent_id),
        ).fetchone()
        return dict(row) if row else None

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
        if new_status in TERMINAL_STATUSES or new_status in {"SUBMITTED", "PARTIALLY_FILLED"}:
            self._resolve_alerts(intent["intent_id"])
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
        Never creates a second client_order_id for the same intent,
        and never makes a second submission attempt for it: once
        place_order_fn has been called once (persisted `attempts` >= 1)
        with no broker_order_id, every later path goes to lookup-only
        reconcile(). A restart, a lease expiry, or a 404 never
        authorizes another attempt.
        """
        sleep = sleep_fn or time.sleep
        intent_id = intent["intent_id"]
        account = intent.get("account") or "paper"
        symbol = intent["symbol"]
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
                current, lookup_fn, grace_seconds=grace_seconds,
                max_polls=max_polls, sleep_fn=sleep,
            )
        if current["status"] == "BLOCKED":
            # A previously refused intent: proceed only if the block
            # is gone; otherwise return it untouched, still BLOCKED.
            if self._blocking_intent(symbol, account, intent_id) is not None:
                return current
            self._update(intent_id, status="PENDING", last_error="")
            current = self._fetch(intent_id)
        # Another intent for this (account, symbol) is UNKNOWN: refuse
        # this conflicting order outright — persist it as BLOCKED and
        # never call place_order_fn for it.
        blocker = self._blocking_intent(symbol, account, intent_id)
        if blocker is not None:
            self._update(
                intent_id, status="BLOCKED",
                last_error=f"blocked by unresolved intent {blocker['intent_id']} for {symbol}",
                claimed_by=None, claim_expires_at=None,
            )
            return self._fetch(intent_id)

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
                        current, lookup_fn, grace_seconds=grace_seconds,
                        max_polls=max_polls, sleep_fn=sleep,
                    )
                if current["status"] == "PENDING":
                    break  # claim expired / released; try to claim below
            if not self._try_claim(intent_id, claimed_by):
                # Still held by a live worker; return the intent as-is
                # rather than submitting a duplicate.
                return self._fetch(intent_id)

        # Re-fetch after claiming: a previous worker (or a pre-crash
        # attempt by this same intent) may already have submitted.
        # attempts >= 1 with no broker_order_id means the one allowed
        # attempt was already made — reconcile only, never place again.
        current = self._fetch(intent_id)
        if (current.get("attempts") or 0) >= 1 and not current.get("broker_order_id"):
            self._update(intent_id, status="UNKNOWN", claimed_by=None, claim_expires_at=None)
            return self.reconcile(
                self._fetch(intent_id), lookup_fn,
                grace_seconds=grace_seconds, max_polls=max_polls, sleep_fn=sleep,
            )

        order = {
            "symbol": intent["symbol"],
            "side": intent["side"],
            "qty": intent["qty"],
            "order_type": intent["order_type"],
            "client_order_id": intent_id,
        }
        if intent.get("limit_price") is not None:
            order["limit_price"] = intent["limit_price"]

        # Persist the attempt BEFORE calling the broker, so a crash
        # mid-call still leaves attempts == 1 on disk and no later
        # restart can authorise a second attempt.
        self._update(intent_id, attempts=(current.get("attempts") or 0) + 1)
        try:
            result = place_order_fn(order)
        except Exception as exc:  # noqa: BLE001 - classify, then recover
            self._update(intent_id, last_error=str(exc))
            if _is_explicit_rejection(exc):
                if "409" in str(exc) or "duplicate" in str(exc).lower():
                    # Conflict: the ID may already exist. Reconcile the
                    # existing order FIRST — lookup with grace polls.
                    # If found, adopt it; if inconclusive, stay UNKNOWN
                    # (alerted, symbol blocked), never REJECTED.
                    return self.reconcile(
                        self._fetch(intent_id), lookup_fn,
                        grace_seconds=grace_seconds, max_polls=max_polls, sleep_fn=sleep,
                    )
                # Non-duplicate explicit rejection (400/validation):
                # the broker conclusively refused the order itself.
                # No order exists, so REJECTED is safe — and the
                # symbol is unblocked (no UNKNOWN intent remains).
                self._update(intent_id, status="REJECTED", claimed_by=None, claim_expires_at=None)
                return self._fetch(intent_id)
            # Ambiguous: timeout / connection loss. The broker may have
            # accepted it. Mark UNKNOWN and reconcile with the same ID.
            self._update(intent_id, status="UNKNOWN", claimed_by=None, claim_expires_at=None)
            return self.reconcile(
                self._fetch(intent_id), lookup_fn,
                grace_seconds=grace_seconds, max_polls=max_polls, sleep_fn=sleep,
            )

        if result is None:
            # place_order_fn returned None without raising: treat as
            # ambiguous (nothing was confirmed).
            self._update(intent_id, status="UNKNOWN", claimed_by=None, claim_expires_at=None)
            return self.reconcile(
                self._fetch(intent_id), lookup_fn,
                grace_seconds=grace_seconds, max_polls=max_polls, sleep_fn=sleep,
            )
        return self._adopt_broker_order(self._fetch(intent_id), result)

    def reconcile(
        self,
        intent: dict,
        lookup_fn: Callable[[str], Optional[dict]] | None = None,
        grace_seconds: float = 5.0,
        max_polls: int = 3,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> dict:
        """Recover an UNKNOWN intent by lookup ONLY, using the same
        client_order_id.

        Polls lookup_fn up to max_polls times (sleeping grace_seconds
        between polls) and adopts the order if found. If the lookup
        stays inconclusive, the intent stays UNKNOWN, a persistent
        alert is recorded, and conflicting orders for that (account,
        symbol) are blocked — the next reconcile pass, or a human,
        resolves it. This method NEVER submits an order: there is no
        resubmit path. A 404 "not found" is inconclusive, not proof
        the broker never accepted the first attempt.
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

        # Lookup stayed inconclusive: stay UNKNOWN, alert, block the
        # symbol. No second submission — ever.
        self._update(intent_id, status="UNKNOWN", claimed_by=None, claim_expires_at=None)
        current = self._fetch(intent_id)
        self._record_alert(
            intent_id,
            current.get("account") or "paper",
            current["symbol"],
            f"lookup inconclusive after {max(1, max_polls)} polls; "
            f"no second submission made; {current['symbol']} blocked "
            f"for account {current.get('account') or 'paper'}",
        )
        return self._fetch(intent_id)

    def reconcile_all_unknowns(
        self,
        lookup_fn: Callable[[str], Optional[dict]] | None = None,
        symbol: str | None = None,
        grace_seconds: float = 0.0,
        max_polls: int = 3,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> list[dict]:
        """Startup reconcile: recover every PENDING / expired-CLAIMED /
        UNKNOWN intent (optionally only for one symbol) before any new
        order for that symbol is accepted. Lookup-only: never submits.
        BLOCKED intents are not picked up here — they wait until their
        blocking UNKNOWN intent resolves, then a later submit proceeds."""
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
                    dict(row), lookup_fn,
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
