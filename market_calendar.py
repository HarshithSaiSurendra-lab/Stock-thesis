"""NYSE exchange calendar: the latest completed trading session.

Problem it solves (user instruction, 2026-11-25): daily-indicator
freshness must be judged against the session the exchange actually
completed — not a fixed "N days old" heuristic. A 7-calendar-day rule
cannot tell a holiday weekend from a missing feed: on the Saturday
after Thanksgiving the latest completed session is Friday Nov 27
(early close 13:00 ET), while a frame ending Wednesday Nov 25 is
missing a whole session and must block.

Uses pandas_market_calendars (NYSE), a maintained library, because the
trading calendar — holidays, early closes — is externally defined and
must not be hand-maintained. Verified against NYSE's published 2026
calendar (see test_data_freshness.py): Thanksgiving Nov 26 closed,
Nov 27 early close 13:00 ET, Christmas Dec 25 closed.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from typing import Optional
from zoneinfo import ZoneInfo

try:
    import pandas_market_calendars as mcal
except ImportError as exc:  # pragma: no cover - exercised by install, not tests
    raise ImportError(
        "pandas_market_calendars is required for the exchange calendar "
        "(pip install -r requirements.txt)"
    ) from exc


def _as_tz_aware(value: datetime, tz: ZoneInfo) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=tz)
    return value.astimezone(tz)


@lru_cache(maxsize=32)
def _nyse_schedule(start_iso: str, end_iso: str):
    """NYSE schedule (market open/close per trading day) for a date range.

    Cached: the schedule is a pure function of the date range, and the
    daily run asks for it once per bar fetch.
    """
    calendar = mcal.get_calendar("NYSE")
    return calendar.schedule(start_date=start_iso, end_date=end_iso)


def latest_completed_session(
    now: Optional[datetime] = None,
    tz: str = "America/New_York",
) -> date:
    """The latest NYSE session whose market close is at or before `now`.

    `now` defaults to the current time. A session counts as completed
    once its close has passed — so at 16:15 ET on a normal day the
    expected session is today (close 16:00), at 10:00 ET it is the
    previous trading day, and on an early-close day (13:00) it is today
    any time after 13:00. Weekends and holidays return the previous
    trading day.
    """
    zone = ZoneInfo(tz)
    moment = _as_tz_aware(now or datetime.now(zone), zone)
    end = moment.date()
    for lookback_days in (21, 60):
        start = end - timedelta(days=lookback_days)
        schedule = _nyse_schedule(start.isoformat(), end.isoformat())
        if schedule.empty:
            continue
        closes = schedule["market_close"]
        completed = closes[closes <= moment]
        if not completed.empty:
            return completed.index[-1].date()
    raise ValueError(f"no completed NYSE session found before {moment.isoformat()}")


def session_close(session: date, tz: str = "America/New_York") -> datetime:
    """The market-close timestamp of a session, in `tz`."""
    zone = ZoneInfo(tz)
    schedule = _nyse_schedule(session.isoformat(), session.isoformat())
    if schedule.empty:
        raise ValueError(f"{session.isoformat()} is not an NYSE trading day")
    close = schedule["market_close"].iloc[0]
    return close.astimezone(zone)


def is_trading_day(day: date) -> bool:
    """True if `day` is an NYSE trading day (open, incl. early closes)."""
    schedule = _nyse_schedule(day.isoformat(), day.isoformat())
    return not schedule.empty


def session_close_time(session: date, tz: str = "America/New_York") -> time:
    """The market-close wall-clock time of a session (e.g. 13:00 early)."""
    return session_close(session, tz).timetz()


def missing_sessions(observed: date, expected: date) -> list[date]:
    """Trading days in (observed, expected] — the sessions with no data."""
    if observed >= expected:
        return []
    schedule = _nyse_schedule(observed.isoformat(), expected.isoformat())
    return [d.date() for d in schedule.index if observed < d.date() <= expected]
