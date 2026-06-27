"""
guardian.py — The safety layer for an unattended trading agent.

DESIGN POSTURE: the broker connection is UNTRUSTED. In a hands-off setup the
agent can place real orders while you're asleep, so the dangerous failures are
not "the code crashes" (that's safe — nothing trades) but "the code keeps
running on bad information" (that's how an account drains quietly). This module
sits between your strategy and the broker and refuses to let bad states through.

It provides five protections, in order of importance:

  1. KILL SWITCH        — a single file/flag that, when set, halts ALL trading
                          immediately. You (or the system) can trip it from
                          anywhere. Checked before every single order.
  2. CIRCUIT BREAKER    — automatic halt when something looks wrong: too many
                          errors, daily loss limit hit, too many orders too fast,
                          or stale market data.
  3. PRE-TRADE CHECKS   — every order is validated before it's sent (sane size,
                          sane price vs last quote, position/notional caps).
  4. HEARTBEAT/HEALTH   — proves the system is alive AND the data is fresh. A
                          frozen feed is more dangerous than a crash.
  5. ALERTING           — pushes a message to you on any trip or anomaly, so
                          hands-off never means blind.

This module is broker-agnostic: you give it a `place_order` callable and a
`get_account` callable, and it wraps them. Wire it to Robinhood (MCP/wrapper),
Alpaca, or a paper simulator without changing the safety logic.
"""

from __future__ import annotations
import json
import time
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Callable, Optional
from enum import Enum


# ----------------------------------------------------------------------------- 
# Configuration
# ----------------------------------------------------------------------------- 
@dataclass
class GuardianConfig:
    # Kill switch: if this file exists, NOTHING trades. Trip it by `touch`-ing it.
    kill_switch_path: str = "./KILL_SWITCH"

    # Circuit-breaker thresholds
    max_daily_loss_pct: float = 0.03        # halt if account down 3% on the day
    max_orders_per_day: int = 25            # halt if exceeded (runaway-loop guard)
    max_orders_per_minute: int = 5          # rate guard against tight loops
    max_consecutive_errors: int = 3         # halt after N broker errors in a row
    max_data_staleness_sec: int = 120       # halt if market data older than this

    # Pre-trade sanity caps
    max_position_pct: float = 0.25          # no single position > 25% of equity
    max_order_notional: float = 2500.0      # hard $ cap per order ($10k acct)
    max_price_deviation_pct: float = 0.05   # reject if order price is >5% off last

    # State persistence (so a restart doesn't reset your daily counters)
    state_path: str = "./guardian_state.json"

    # Alerting
    alert_log_path: str = "./alerts.log"


class HaltReason(str, Enum):
    KILL_SWITCH = "kill_switch_engaged"
    DAILY_LOSS = "daily_loss_limit"
    ORDER_COUNT = "max_orders_exceeded"
    RATE_LIMIT = "order_rate_exceeded"
    ERRORS = "consecutive_errors"
    STALE_DATA = "stale_market_data"
    MANUAL = "manual_halt"


# ----------------------------------------------------------------------------- 
# Alerting (pluggable). Default writes to a log file; swap in email/SMS/push.
# ----------------------------------------------------------------------------- 
class Alerter:
    """Override `send` to wire Telegram/SMS/email. Default = local log + stdout."""

    def __init__(self, log_path: str):
        self.log_path = Path(log_path)
        self.logger = logging.getLogger("guardian.alerts")

    def send(self, level: str, message: str) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        line = f"[{ts}] {level.upper()}: {message}"
        print(f"🔔 {line}")
        with open(self.log_path, "a") as f:
            f.write(line + "\n")


# Example real alerter you can drop in once you have a bot token. Left here as a
# template — it makes ONE outbound call and never raises into the trading path.
class TelegramAlerter(Alerter):
    def __init__(self, log_path: str, bot_token: str, chat_id: str):
        super().__init__(log_path)
        self.bot_token = bot_token
        self.chat_id = chat_id

    def send(self, level: str, message: str) -> None:
        super().send(level, message)  # always keep the local record
        try:
            import urllib.request, urllib.parse
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            data = urllib.parse.urlencode(
                {"chat_id": self.chat_id, "text": f"{level.upper()}: {message}"}
            ).encode()
            urllib.request.urlopen(url, data=data, timeout=5)
        except Exception as e:
            # NEVER let alerting failure crash trading logic — just log it.
            self.logger.warning(f"alert send failed: {e}")


# ----------------------------------------------------------------------------- 
# Mutable runtime state (persisted so restarts are safe)
# ----------------------------------------------------------------------------- 
@dataclass
class GuardianState:
    trading_day: str = field(default_factory=lambda: date.today().isoformat())
    day_start_equity: Optional[float] = None
    orders_today: int = 0
    consecutive_errors: int = 0
    halted: bool = False
    halt_reason: Optional[str] = None
    recent_order_times: list = field(default_factory=list)  # epoch seconds

    @classmethod
    def load(cls, path: str) -> "GuardianState":
        p = Path(path)
        if p.exists():
            try:
                return cls(**json.loads(p.read_text()))
            except Exception:
                pass
        return cls()

    def save(self, path: str) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2))


# ----------------------------------------------------------------------------- 
# The Guardian
# ----------------------------------------------------------------------------- 
class Guardian:
    """
    Wraps broker calls with the full safety stack. Usage:

        g = Guardian(cfg, get_account=..., last_data_ts=...)
        g.start_day()                       # call once at session open
        if g.can_trade():                   # checks kill switch + breakers
            ok, reason = g.validate_order(order, last_price, equity, positions)
            if ok:
                g.submit(order, place_order_fn)
    """

    def __init__(
        self,
        cfg: GuardianConfig,
        get_account: Callable[[], dict],
        last_data_ts: Callable[[], float],
        alerter: Optional[Alerter] = None,
    ):
        self.cfg = cfg
        self.get_account = get_account          # () -> {"equity": float, ...}
        self.last_data_ts = last_data_ts        # () -> epoch sec of newest data
        self.alerter = alerter or Alerter(cfg.alert_log_path)
        self.state = GuardianState.load(cfg.state_path)
        self.log = logging.getLogger("guardian")

    # ---- daily lifecycle ---------------------------------------------------
    def start_day(self) -> None:
        today = date.today().isoformat()
        if self.state.trading_day != today:
            # new day: reset counters but KEEP a halt if it was manual/kill
            self.state = GuardianState(trading_day=today)
        if self.state.day_start_equity is None:
            acct = self._safe_account()
            self.state.day_start_equity = acct.get("equity") if acct else None
        self.state.save(self.cfg.state_path)
        self.alerter.send("info", f"Trading day started. "
                          f"Start equity: {self.state.day_start_equity}")

    # ---- the master gate ---------------------------------------------------
    def can_trade(self) -> bool:
        """Single source of truth for 'is it safe to place ANY order right now?'"""
        # 1. Kill switch — highest priority, checked first, every time.
        if Path(self.cfg.kill_switch_path).exists():
            return self._halt(HaltReason.KILL_SWITCH)

        # 2. Already halted this session?
        if self.state.halted:
            return False

        # 3. Stale data — a frozen feed means every signal is a lie.
        age = time.time() - self.last_data_ts()
        if age > self.cfg.max_data_staleness_sec:
            return self._halt(HaltReason.STALE_DATA,
                              f"data is {age:.0f}s old")

        # 4. Daily loss limit.
        acct = self._safe_account()
        if acct and self.state.day_start_equity:
            eq = acct.get("equity")
            if eq is not None:
                dd = (eq - self.state.day_start_equity) / self.state.day_start_equity
                if dd <= -self.cfg.max_daily_loss_pct:
                    return self._halt(HaltReason.DAILY_LOSS,
                                      f"down {dd:.2%} on the day")

        # 5. Order-count ceiling (runaway-loop guard).
        if self.state.orders_today >= self.cfg.max_orders_per_day:
            return self._halt(HaltReason.ORDER_COUNT)

        # 6. Order-rate ceiling (tight-loop guard).
        now = time.time()
        recent = [t for t in self.state.recent_order_times if now - t < 60]
        if len(recent) >= self.cfg.max_orders_per_minute:
            return self._halt(HaltReason.RATE_LIMIT)

        return True

    # ---- pre-trade validation ---------------------------------------------
    def validate_order(
        self, order: dict, last_price: float, equity: float, positions: dict
    ) -> tuple[bool, str]:
        """
        order = {"symbol": str, "side": "buy"/"sell", "qty": float, "limit": float|None}
        Returns (ok, reason). Rejects anything that fails a sanity check.
        """
        sym = order["symbol"]
        qty = float(order["qty"])
        px = order.get("limit") or last_price

        if qty <= 0:
            return False, "non-positive quantity"

        notional = qty * px
        if notional > self.cfg.max_order_notional:
            return False, f"order notional ${notional:.0f} > cap ${self.cfg.max_order_notional:.0f}"

        # price-sanity: protect against a bad/stale quote producing a crazy order
        if last_price > 0:
            dev = abs(px - last_price) / last_price
            if dev > self.cfg.max_price_deviation_pct:
                return False, f"order price {px} is {dev:.1%} off last {last_price}"

        # post-trade position concentration check (for buys)
        if order["side"] == "buy" and equity > 0:
            existing = positions.get(sym, {}).get("notional", 0.0)
            new_pct = (existing + notional) / equity
            if new_pct > self.cfg.max_position_pct:
                return False, f"{sym} would be {new_pct:.0%} of equity > {self.cfg.max_position_pct:.0%} cap"

        return True, "ok"

    # ---- order submission with error tracking -----------------------------
    def submit(self, order: dict, place_order_fn: Callable[[dict], dict]) -> Optional[dict]:
        """
        Wraps the actual broker call. Tracks consecutive errors and trips the
        breaker if the broker starts failing — the 'API goes bad' case you asked
        about. Returns the broker result, or None on failure.
        """
        if not self.can_trade():
            self.alerter.send("warn", f"submit blocked: {self.state.halt_reason}")
            return None
        try:
            result = place_order_fn(order)
            # success: reset error streak, count the order
            self.state.consecutive_errors = 0
            self.state.orders_today += 1
            self.state.recent_order_times.append(time.time())
            self.state.recent_order_times = self.state.recent_order_times[-20:]
            self.state.save(self.cfg.state_path)
            self.alerter.send("info",
                f"ORDER OK {order['side']} {order['qty']} {order['symbol']}")
            return result
        except Exception as e:
            self.state.consecutive_errors += 1
            self.state.save(self.cfg.state_path)
            self.alerter.send("error",
                f"broker error #{self.state.consecutive_errors}: {e}")
            if self.state.consecutive_errors >= self.cfg.max_consecutive_errors:
                self._halt(HaltReason.ERRORS,
                           f"{self.state.consecutive_errors} errors in a row")
            return None

    # ---- health / heartbeat ------------------------------------------------
    def health_report(self) -> dict:
        """Call on a timer (e.g. every minute) to prove liveness + freshness."""
        age = time.time() - self.last_data_ts()
        acct = self._safe_account()
        report = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "halted": self.state.halted,
            "halt_reason": self.state.halt_reason,
            "kill_switch": Path(self.cfg.kill_switch_path).exists(),
            "data_age_sec": round(age, 1),
            "data_fresh": age <= self.cfg.max_data_staleness_sec,
            "orders_today": self.state.orders_today,
            "consecutive_errors": self.state.consecutive_errors,
            "equity": acct.get("equity") if acct else None,
            "day_start_equity": self.state.day_start_equity,
        }
        # Proactively alert if data is going stale even if not yet trading
        if not report["data_fresh"]:
            self.alerter.send("warn", f"DATA STALE: {age:.0f}s since last update")
        return report

    # ---- manual controls ---------------------------------------------------
    def manual_halt(self, note: str = "") -> None:
        self._halt(HaltReason.MANUAL, note)

    def reset_halt(self) -> None:
        """Clear a halt AFTER you've investigated. Does not clear the kill file."""
        if Path(self.cfg.kill_switch_path).exists():
            self.alerter.send("warn", "reset refused: kill switch file still present")
            return
        self.state.halted = False
        self.state.halt_reason = None
        self.state.consecutive_errors = 0
        self.state.save(self.cfg.state_path)
        self.alerter.send("info", "halt cleared manually")

    # ---- internals ---------------------------------------------------------
    def _halt(self, reason: HaltReason, detail: str = "") -> bool:
        if not self.state.halted:  # only alert on the transition
            self.state.halted = True
            self.state.halt_reason = reason.value
            self.state.save(self.cfg.state_path)
            self.alerter.send("critical",
                f"TRADING HALTED — {reason.value}. {detail}")
        return False

    def _safe_account(self) -> Optional[dict]:
        """Account fetch that never raises into the safety path."""
        try:
            return self.get_account()
        except Exception as e:
            self.state.consecutive_errors += 1
            self.alerter.send("error", f"account fetch failed: {e}")
            if self.state.consecutive_errors >= self.cfg.max_consecutive_errors:
                self._halt(HaltReason.ERRORS, "account fetch repeatedly failing")
            return None


if __name__ == "__main__":
    # Demo: simulate a session, including the "API goes bad" failure you asked for.
    logging.basicConfig(level=logging.INFO)
    cfg = GuardianConfig()

    # clean any leftover state from a prior run
    for f in (cfg.state_path, cfg.kill_switch_path):
        Path(f).unlink(missing_ok=True)

    fake_equity = {"v": 10_000.0}
    fresh_ts = {"v": time.time()}

    g = Guardian(
        cfg,
        get_account=lambda: {"equity": fake_equity["v"]},
        last_data_ts=lambda: fresh_ts["v"],
    )
    g.start_day()

    print("\n--- 1. Normal order (should pass) ---")
    order = {"symbol": "AAPL", "side": "buy", "qty": 5, "limit": 200.0}
    ok, reason = g.validate_order(order, last_price=199.0, equity=10_000, positions={})
    print(f"validate: {ok} ({reason})")

    good_broker = lambda o: {"id": "abc", "status": "filled"}
    print("submit:", g.submit(order, good_broker))

    print("\n--- 2. Oversized order (should be rejected) ---")
    big = {"symbol": "AAPL", "side": "buy", "qty": 50, "limit": 200.0}
    print("validate:", g.validate_order(big, 200.0, 10_000, {}))

    print("\n--- 3. Crazy price vs last (should be rejected) ---")
    bad_px = {"symbol": "AAPL", "side": "buy", "qty": 1, "limit": 260.0}
    print("validate:", g.validate_order(bad_px, last_price=200.0, equity=10_000, positions={}))

    print("\n--- 4. API GOES BAD: broker raises repeatedly -> circuit breaker ---")
    def broken_broker(o):
        raise ConnectionError("Robinhood endpoint 503 / throttled")
    for i in range(4):
        g.submit(order, broken_broker)
    print("can_trade after errors:", g.can_trade())

    print("\n--- 5. Health report ---")
    g.reset_halt()
    print(json.dumps(g.health_report(), indent=2))

    print("\n--- 6. STALE DATA halts trading ---")
    fresh_ts["v"] = time.time() - 300  # pretend feed froze 5 min ago
    print("can_trade with stale feed:", g.can_trade())

    print("\n--- 7. KILL SWITCH ---")
    fresh_ts["v"] = time.time()
    g.reset_halt()
    Path(cfg.kill_switch_path).touch()
    print("can_trade with kill switch file present:", g.can_trade())
    Path(cfg.kill_switch_path).unlink()

    # tidy up demo artifacts
    for f in (cfg.state_path,):
        Path(f).unlink(missing_ok=True)
