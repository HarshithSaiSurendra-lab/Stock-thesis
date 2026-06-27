"""
memory.py — Trade journal, retrieval memory, and pattern flagging.

What this does (and deliberately does NOT do):
  - RECORDS every decision: the signal state, liquidity, intended vs actual fill,
    paper-vs-live gap, and the eventual closed-trade outcome.
  - RETRIEVES similar past setups BEFORE acting, so the system references real
    history ("setups like this lost 9 of 12 times") instead of guessing.
  - FLAGS patterns: warns when a new setup resembles past losers.
  - TRACKS tax cost-basis per lot for realized gains/losses.

It does NOT retrain a model in real time off recent trades. That chases noise.
Learning is deliberate: you periodically export this journal and review/retrain
on a sample large enough to mean something. The memory makes mistakes VISIBLE;
acting on them stays human-gated until there's real data.

Storage is a local SQLite file — no dependencies, durable, queryable.
"""

from __future__ import annotations
import sqlite3
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT,              -- strong_up / mild_up / etc.
    -- signal snapshot at decision time (your six indicators + extras)
    rsi REAL, mfi REAL, kvo_hist REAL, obv_slope REAL, wad_slope REAL,
    momentum REAL, rvol REAL,
    -- liquidity snapshot
    spread_pct REAL, dollar_volume REAL,
    -- order intent vs reality
    order_type TEXT, intended_price REAL,
    paper_fill REAL, live_fill REAL, slippage_bps REAL,
    qty REAL,
    -- outcome (filled in when the position closes)
    closed INTEGER DEFAULT 0,
    exit_ts TEXT, exit_price REAL, realized_pl REAL, holding_days REAL,
    -- did it match what the signal predicted?
    predicted_up INTEGER, actually_up INTEGER
);

CREATE TABLE IF NOT EXISTS tax_lots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    open_ts TEXT NOT NULL,
    qty REAL NOT NULL,
    cost_basis REAL NOT NULL,        -- total $ paid incl. our recorded fill
    close_ts TEXT,
    proceeds REAL,
    realized_gain REAL,
    term TEXT                        -- 'short' / 'long' (held >365d)
);
"""


@dataclass
class SignalSnapshot:
    symbol: str
    direction: str
    rsi: float
    mfi: float
    kvo_hist: float
    obv_slope: float
    wad_slope: float
    momentum: float
    rvol: float
    spread_pct: float
    dollar_volume: float


class TradeMemory:
    def __init__(self, db_path: str = "./trade_memory.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- recording ---------------------------------------------------------
    def log_decision(self, snap: SignalSnapshot, order_type: str,
                     intended_price: float, qty: float,
                     paper_fill: Optional[float] = None,
                     live_fill: Optional[float] = None) -> int:
        slippage_bps = None
        if paper_fill and live_fill and paper_fill > 0:
            slippage_bps = (live_fill - paper_fill) / paper_fill * 1e4
        cur = self.conn.execute(
            """INSERT INTO decisions
               (ts, symbol, direction, rsi, mfi, kvo_hist, obv_slope, wad_slope,
                momentum, rvol, spread_pct, dollar_volume, order_type,
                intended_price, paper_fill, live_fill, slippage_bps, qty,
                predicted_up)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (datetime.now(timezone.utc).isoformat(), snap.symbol, snap.direction,
             snap.rsi, snap.mfi, snap.kvo_hist, snap.obv_slope, snap.wad_slope,
             snap.momentum, snap.rvol, snap.spread_pct, snap.dollar_volume,
             order_type, intended_price, paper_fill, live_fill, slippage_bps,
             qty, 1 if "up" in snap.direction else 0),
        )
        self.conn.commit()
        return cur.lastrowid

    def close_decision(self, decision_id: int, exit_price: float,
                       realized_pl: float, holding_days: float) -> None:
        row = self.conn.execute(
            "SELECT intended_price FROM decisions WHERE id=?", (decision_id,)
        ).fetchone()
        actually_up = 1 if (row and exit_price > row["intended_price"]) else 0
        self.conn.execute(
            """UPDATE decisions SET closed=1, exit_ts=?, exit_price=?,
               realized_pl=?, holding_days=?, actually_up=? WHERE id=?""",
            (datetime.now(timezone.utc).isoformat(), exit_price, realized_pl,
             holding_days, actually_up, decision_id),
        )
        self.conn.commit()

    # ---- retrieval BEFORE acting ------------------------------------------
    def similar_setups(self, snap: SignalSnapshot, k: int = 20) -> dict:
        """
        Find closed trades with a similar signal fingerprint and report how they
        turned out. This is the 'reference its mistakes' core — retrieval, not
        retraining. Similarity = normalized distance over the indicator vector.
        """
        rows = self.conn.execute(
            "SELECT * FROM decisions WHERE closed=1"
        ).fetchall()
        if not rows:
            return {"n": 0, "msg": "no closed history yet"}

        def dist(r) -> float:
            # simple normalized L2 over the indicators we trust most
            terms = [
                ((r["rsi"] or 50) - snap.rsi) / 100,
                ((r["mfi"] or 50) - snap.mfi) / 100,
                math.tanh(((r["momentum"] or 0) - snap.momentum)),
                math.tanh(((r["rvol"] or 0) - snap.rvol)),
            ]
            return math.sqrt(sum(t * t for t in terms))

        ranked = sorted(rows, key=dist)[:k]
        wins = [r for r in ranked if (r["realized_pl"] or 0) > 0]
        losses = [r for r in ranked if (r["realized_pl"] or 0) <= 0]
        avg_pl = sum((r["realized_pl"] or 0) for r in ranked) / len(ranked)
        return {
            "n": len(ranked),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(ranked), 3),
            "avg_realized_pl": round(avg_pl, 2),
            "verdict": self._verdict(len(wins), len(ranked), avg_pl),
        }

    def _verdict(self, wins: int, n: int, avg_pl: float) -> str:
        if n < 5:
            return "insufficient history — proceed but log"
        wr = wins / n
        if wr < 0.4 and avg_pl < 0:
            return "CAUTION: setups like this have mostly lost money"
        if wr > 0.6 and avg_pl > 0:
            return "favorable: similar setups have tended to profit"
        return "mixed: no clear historical edge for this setup"

    def flag_if_repeating_loss(self, snap: SignalSnapshot,
                               threshold: float = 0.4) -> Optional[str]:
        """Return a warning string if this setup resembles past losers."""
        sim = self.similar_setups(snap)
        if sim["n"] >= 5 and sim["win_rate"] < threshold and sim["avg_realized_pl"] < 0:
            return (f"PATTERN FLAG {snap.symbol}: resembles {sim['n']} past trades, "
                    f"{sim['losses']} lost, avg {sim['avg_realized_pl']}. "
                    f"Consider skipping.")
        return None

    # ---- tax tracking ------------------------------------------------------
    def open_tax_lot(self, symbol: str, qty: float, cost_basis: float) -> int:
        cur = self.conn.execute(
            "INSERT INTO tax_lots (symbol, open_ts, qty, cost_basis) VALUES (?,?,?,?)",
            (symbol, datetime.now(timezone.utc).isoformat(), qty, cost_basis),
        )
        self.conn.commit()
        return cur.lastrowid

    def close_tax_lot(self, lot_id: int, proceeds: float) -> dict:
        row = self.conn.execute("SELECT * FROM tax_lots WHERE id=?", (lot_id,)).fetchone()
        if not row:
            return {}
        gain = proceeds - row["cost_basis"]
        open_dt = datetime.fromisoformat(row["open_ts"])
        held_days = (datetime.now(timezone.utc) - open_dt).days
        term = "long" if held_days > 365 else "short"
        self.conn.execute(
            "UPDATE tax_lots SET close_ts=?, proceeds=?, realized_gain=?, term=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), proceeds, gain, term, lot_id),
        )
        self.conn.commit()
        return {"symbol": row["symbol"], "realized_gain": round(gain, 2), "term": term}

    def tax_summary(self, year: Optional[int] = None) -> dict:
        rows = self.conn.execute(
            "SELECT * FROM tax_lots WHERE close_ts IS NOT NULL"
        ).fetchall()
        short = sum(r["realized_gain"] for r in rows if r["term"] == "short")
        long = sum(r["realized_gain"] for r in rows if r["term"] == "long")
        return {"short_term_gain": round(short, 2), "long_term_gain": round(long, 2),
                "total_realized": round(short + long, 2), "closed_lots": len(rows)}

    # ---- daily report ------------------------------------------------------
    def daily_summary(self) -> dict:
        today = datetime.now(timezone.utc).date().isoformat()
        opened = self.conn.execute(
            "SELECT COUNT(*) c FROM decisions WHERE ts LIKE ?", (today + "%",)
        ).fetchone()["c"]
        closed = self.conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(realized_pl),0) pl "
            "FROM decisions WHERE exit_ts LIKE ?", (today + "%",)
        ).fetchone()
        slip = self.conn.execute(
            "SELECT AVG(slippage_bps) s FROM decisions WHERE ts LIKE ? "
            "AND slippage_bps IS NOT NULL", (today + "%",)
        ).fetchone()["s"]
        return {
            "date": today,
            "trades_opened": opened,
            "trades_closed": closed["c"],
            "realized_pl_today": round(closed["pl"], 2),
            "avg_slippage_bps": round(slip, 1) if slip is not None else None,
        }


if __name__ == "__main__":
    import os
    Path("./trade_memory.db").unlink(missing_ok=True)
    m = TradeMemory()

    # simulate a few past closed trades to give retrieval something to find
    import random
    random.seed(1)
    for i in range(15):
        snap = SignalSnapshot(
            symbol=f"TST{i%4}", direction="strong_up",
            rsi=random.uniform(40, 75), mfi=random.uniform(40, 75),
            kvo_hist=random.uniform(-1, 1), obv_slope=random.uniform(-1, 1),
            wad_slope=random.uniform(-1, 1), momentum=random.uniform(-0.1, 0.2),
            rvol=random.uniform(0.1, 0.5), spread_pct=0.001,
            dollar_volume=5e6,
        )
        did = m.log_decision(snap, "market", 100.0, 5, paper_fill=100.0,
                             live_fill=100.0 + random.uniform(0, 0.3))
        # close most of them, biased to losses for high-RSI entries
        pl = random.uniform(-8, 4) if snap.rsi > 65 else random.uniform(-3, 7)
        m.close_decision(did, exit_price=100 + pl/5, realized_pl=pl,
                         holding_days=random.uniform(1, 14))

    print("=== Retrieval before acting on a new high-RSI setup ===")
    new = SignalSnapshot("NEW", "strong_up", rsi=72, mfi=70, kvo_hist=0.5,
                         obv_slope=0.2, wad_slope=0.1, momentum=0.15, rvol=0.4,
                         spread_pct=0.001, dollar_volume=5e6)
    print(json.dumps(m.similar_setups(new), indent=2))
    flag = m.flag_if_repeating_loss(new)
    print("\nPattern flag:", flag or "none")

    print("\n=== Tax tracking ===")
    lot = m.open_tax_lot("AAPL", 5, 1000.0)
    print("closed lot:", m.close_tax_lot(lot, proceeds=1080.0))
    print("tax summary:", m.tax_summary())

    print("\n=== Daily summary ===")
    print(json.dumps(m.daily_summary(), indent=2))
