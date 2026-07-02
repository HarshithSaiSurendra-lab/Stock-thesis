from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def parse_quote_timestamp(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            cleaned = value.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(cleaned)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def quote_age_seconds(quote: dict, now: Optional[datetime] = None) -> Optional[float]:
    ts = parse_quote_timestamp(quote.get("timestamp") or quote.get("t"))
    if ts is None:
        return None
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return max(0.0, (now - ts.astimezone(timezone.utc)).total_seconds())


def quote_mid(quote: dict) -> float:
    return (float(quote["bid"]) + float(quote["ask"])) / 2
