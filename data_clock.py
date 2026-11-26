"""Tracks the newest timestamp actually supplied by the data feed.

The Guardian's staleness check must judge the feed, not the wall
clock: a quote received just now with a 30-minute-old feed timestamp
is stale data, and a run that has not observed any feed data yet has
nothing to judge — its age is unknown, not zero.

Only quote timestamps feed this clock. Daily bars carry a session
date, not an arrival time, and are judged against the exchange
calendar instead (market_calendar.py).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from execution_quality import parse_quote_timestamp


class DataClock:
    """Newest feed timestamp observed this session (epoch seconds)."""

    def __init__(self) -> None:
        self._latest: Optional[float] = None

    def note(self, value) -> None:
        """Record a feed timestamp (datetime, ISO string, or epoch)."""
        ts = parse_quote_timestamp(value) if not isinstance(value, (int, float)) else value
        if ts is None:
            return
        epoch = ts.timestamp() if isinstance(ts, datetime) else float(ts)
        if self._latest is None or epoch > self._latest:
            self._latest = epoch

    def last_ts(self) -> Optional[float]:
        """Epoch seconds of the newest feed timestamp, or None if none seen."""
        return self._latest

    def last_dt(self) -> Optional[datetime]:
        """Newest feed timestamp as an aware datetime, or None."""
        if self._latest is None:
            return None
        return datetime.fromtimestamp(self._latest, tz=timezone.utc)

    def age_seconds(self, now: Optional[datetime] = None) -> Optional[float]:
        """Seconds since the newest feed timestamp, or None if none seen."""
        if self._latest is None:
            return None
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return max(0.0, moment.timestamp() - self._latest)
