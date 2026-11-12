from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


def weekly_slot(timestamp, timezone="Europe/Podgorica"):
    current = datetime.fromtimestamp(timestamp, ZoneInfo(timezone))
    due = (current - timedelta(days=current.weekday())).replace(hour=12, minute=0, second=0, microsecond=0)
    if current < due:
        due -= timedelta(days=7)
    return due.date().isoformat()


def review_slot(timestamp, timezone):
    """Replay only the latest due calendar slot, never a backlog of ad changes."""
    now = datetime.fromtimestamp(timestamp, ZoneInfo(timezone))
    day = now.date() - timedelta(days=int(now.hour < 12))
    monday = date.fromisoformat(weekly_slot(timestamp, timezone))
    anchor = date(2026, 11, 12)
    if day < anchor:
        return monday.isoformat(), True, False
    light = day - timedelta(days=(day - anchor).days % 3)
    due = max(monday, light)
    return due.isoformat(), due == monday, due == light
