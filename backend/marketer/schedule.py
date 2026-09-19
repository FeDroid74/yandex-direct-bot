from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def weekly_slot(timestamp, timezone="Europe/Podgorica"):
    current = datetime.fromtimestamp(timestamp, ZoneInfo(timezone))
    due = (current - timedelta(days=current.weekday())).replace(hour=12, minute=0, second=0, microsecond=0)
    if current < due:
        due -= timedelta(days=7)
    return due.date().isoformat()
