from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from after_hours import run_after_hours
from config import TradingConfig
from strategy_runner import run_daily, _write_decision_log


SessionName = Literal["regular", "after_hours", "closed"]
RunMode = Literal["auto", "regular", "after-hours"]


@dataclass(frozen=True)
class SessionDecision:
    session: SessionName
    reason: str
    local_time: str


def classify_session(broker, cfg: TradingConfig, now: Optional[datetime] = None) -> SessionDecision:
    tz = ZoneInfo(cfg.run.timezone)
    now = now or datetime.now(tz)
    local_now = now.astimezone(tz) if now.tzinfo else now.replace(tzinfo=tz)

    market_open = broker.is_market_open() if hasattr(broker, "is_market_open") else False
    if market_open:
        return SessionDecision("regular", "broker clock says regular market is open", local_now.isoformat())

    is_weekday = local_now.weekday() < 5
    after_hours_open = time(16, 0) <= local_now.time() < time(20, 0)
    if is_weekday and after_hours_open:
        return SessionDecision("after_hours", "regular market closed and after-hours window is open", local_now.isoformat())

    return SessionDecision("closed", "outside regular and after-hours windows", local_now.isoformat())


def run_routed(
    broker,
    guardian,
    memory,
    cfg: TradingConfig,
    *,
    dry_run: bool = False,
    mode: RunMode = "auto",
) -> dict:
    if mode == "regular":
        result = run_daily(broker, guardian, memory, cfg, dry_run=dry_run)
        result.setdefault("router", {"mode": mode, "session": "regular", "reason": "forced regular mode"})
        return result
    if mode == "after-hours":
        result = run_after_hours(broker, guardian, memory, cfg, dry_run=dry_run)
        result.setdefault("router", {"mode": mode, "session": "after_hours", "reason": "forced after-hours mode"})
        return result

    decision = classify_session(broker, cfg)
    if decision.session == "regular":
        result = run_daily(broker, guardian, memory, cfg, dry_run=dry_run)
    elif decision.session == "after_hours":
        result = run_after_hours(broker, guardian, memory, cfg, dry_run=dry_run)
    else:
        result = {
            "date": datetime.now().date().isoformat(),
            "status": "closed",
            "session": decision.session,
            "reason": decision.reason,
            "orders": [],
        }
        _write_decision_log(result, cfg)

    result["router"] = {
        "mode": mode,
        "session": decision.session,
        "reason": decision.reason,
        "local_time": decision.local_time,
    }
    return result
